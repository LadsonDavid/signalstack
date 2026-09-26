"""SQLite store.

Design rule (Kleppmann): `signal` is the append-only source of truth. Everything
else is either collector bookkeeping (snapshot/breaker/run) or a derived cache
that can be thrown away and rebuilt. Scores are never stored — they are computed
from this log on read, so retuning malveon.yaml re-scores all history instantly.

Idempotency is a UNIQUE constraint on signal.dedupe_key + INSERT OR IGNORE,
not application logic. Collectors may safely re-emit the same signal forever.
"""
from __future__ import annotations

import contextlib
import json
import re
import sqlite3
import threading
from datetime import datetime, timezone

from .config import DATA_DIR, DB_PATH

_local = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS account (
    id            INTEGER PRIMARY KEY,
    domain        TEXT NOT NULL UNIQUE,
    name          TEXT,
    ats           TEXT,                -- greenhouse|lever|ashby|workable|smartrecruiters
    ats_token     TEXT,
    headcount     INTEGER,             -- only ever set from real enrichment, never guessed
    eng_open_reqs INTEGER DEFAULT 0,
    watchlisted   INTEGER NOT NULL DEFAULT 0,
    disqualified  TEXT,                -- reason string, NULL = eligible
    notes         TEXT,
    first_seen    TEXT NOT NULL,
    last_seen     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_account_watch ON account(watchlisted);

CREATE TABLE IF NOT EXISTS signal (
    id          INTEGER PRIMARY KEY,
    account_id  INTEGER NOT NULL REFERENCES account(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL,         -- key into malveon.yaml fit:/intent:
    source      TEXT NOT NULL,         -- collector name
    detail      TEXT,                  -- human-readable reason, shown in the UI
    url         TEXT,
    value       REAL NOT NULL DEFAULT 1,
    payload     TEXT,
    observed_at TEXT NOT NULL,
    dedupe_key  TEXT NOT NULL UNIQUE
);
CREATE INDEX IF NOT EXISTS ix_signal_acct ON signal(account_id, observed_at DESC);
CREATE INDEX IF NOT EXISTS ix_signal_kind ON signal(kind, observed_at DESC);

-- Last raw state per (collector, key), for snapshot -> diff -> emit.
CREATE TABLE IF NOT EXISTS snapshot (
    collector  TEXT NOT NULL,
    key        TEXT NOT NULL,
    body       TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (collector, key)
);

CREATE TABLE IF NOT EXISTS collector_run (
    id          INTEGER PRIMARY KEY,
    collector   TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    status      TEXT NOT NULL,         -- ok|failed|skipped_open_breaker
    records     INTEGER DEFAULT 0,
    signals     INTEGER DEFAULT 0,
    error       TEXT
);
CREATE INDEX IF NOT EXISTS ix_run ON collector_run(collector, started_at DESC);

-- Circuit breaker state, persisted so it survives restarts.
CREATE TABLE IF NOT EXISTS breaker (
    collector    TEXT PRIMARY KEY,
    failures     INTEGER NOT NULL DEFAULT 0,
    opened_until TEXT
);

CREATE TABLE IF NOT EXISTS contact (
    id         INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL REFERENCES account(id) ON DELETE CASCADE,
    name       TEXT,
    title      TEXT,
    email      TEXT,
    status     TEXT,                   -- valid|invalid|catch_all|unknown|unverified
    source     TEXT NOT NULL,          -- provenance, required for GDPR deletion
    created_at TEXT NOT NULL,
    UNIQUE(account_id, email)
);

-- Verification results are cached forever; re-checked only after 90 days.
CREATE TABLE IF NOT EXISTS email_verification (
    email      TEXT PRIMARY KEY,
    status     TEXT NOT NULL,
    provider   TEXT NOT NULL,
    checked_at TEXT NOT NULL,
    raw        TEXT
);

-- Public posts mined for stated intent. Most cannot be attributed to a company
-- automatically, and guessing would manufacture fake precision — so they live
-- here as a reviewable feed, and only CONFIRMED matches become account signals.
CREATE TABLE IF NOT EXISTS intent_post (
    id           INTEGER PRIMARY KEY,
    source       TEXT NOT NULL,
    ext_id       TEXT NOT NULL,
    author       TEXT,
    url          TEXT,
    title        TEXT,
    body         TEXT,
    created_at   TEXT,
    account_id   INTEGER REFERENCES account(id) ON DELETE SET NULL,
    label        TEXT,               -- stated_intent|competitor_gripe|active_research|none
    confidence   REAL,
    rationale    TEXT,
    reviewed     INTEGER NOT NULL DEFAULT 0,
    UNIQUE(source, ext_id)
);
CREATE INDEX IF NOT EXISTS ix_post_label ON intent_post(label, created_at DESC);

-- malveon.com visitors. Every hit is stored, identified or not: unresolved
-- visits still tell you traffic is real, and the resolver improves over time.
CREATE TABLE IF NOT EXISTS site_visit (
    id         INTEGER PRIMARY KEY,
    ip         TEXT NOT NULL,
    path       TEXT,
    referrer   TEXT,
    user_agent TEXT,
    seconds    REAL,
    org        TEXT,               -- ISP/company name from reverse lookup
    account_id INTEGER REFERENCES account(id) ON DELETE SET NULL,
    resolution TEXT,               -- ptr|ipinfo|none|bot|isp
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_visit ON site_visit(created_at DESC);
CREATE INDEX IF NOT EXISTS ix_visit_acct ON site_visit(account_id);

-- Cache IP -> company so a repeat visitor costs nothing to resolve.
CREATE TABLE IF NOT EXISTS ip_cache (
    ip         TEXT PRIMARY KEY,
    domain     TEXT,
    org        TEXT,
    resolution TEXT,
    checked_at TEXT NOT NULL
);

-- LLM classifications cached by content hash so a post is never paid for twice.
CREATE TABLE IF NOT EXISTS llm_cache (
    hash       TEXT PRIMARY KEY,
    result     TEXT NOT NULL,
    model      TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def conn() -> sqlite3.Connection:
    """One connection per thread. WAL lets collectors write while the UI reads."""
    c = getattr(_local, "c", None)
    if c is None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        c.execute("PRAGMA foreign_keys=ON")
        _local.c = c
    return c


@contextlib.contextmanager
def tx():
    c = conn()
    try:
        yield c
        c.commit()
    except Exception:
        c.rollback()
        raise


_reconciled = False

# Pruned from malveon.yaml's persona pain_terms — turned out to be standard
# job-posting vocabulary ("root cause analysis," "capacity planning," ADRs as
# a good practice a company brags about) rather than real signal, discovered
# by actually reading the pain-points page and seeing nearly every company on
# it show the same handful of generic phrases. Removing them from config
# stops NEW false matches; it does nothing about ones already fired and
# sitting in the signal log, which is what this backfill is for.
REMOVED_PAIN_TERMS = {
    "capacity planning", "escalation path", "team health",
    "root cause", "correlate logs", "what changed",
    "decision log", "decision record", "adr", "architecture decision", "standup status",
}

# competitor_complementary/competitor_displacement dedupe_keys end in
# "<domain>:comp_c:<keyword>" / "<domain>:comp_d:<keyword>" — these bare-word
# keywords got replaced with their actual domain (harness.io, honeycomb.io,
# backstage.io, blameless.com) because the bare word is also common English
# with no connection to the product ("we harness AI," "blameless postmortem"
# is standard SRE vocabulary independent of the Blameless product). The old
# dedupe_key never matches the new one, so the wrong signal just sits there
# forever unless purged here.
STALE_COMPETITOR_KEYWORDS = {"harness", "honeycomb", "backstage", "blameless"}

# These 9 hosting platforms now only get detected by techstack's real header/
# CNAME probe (see malveon.yaml) — job-ad text ("experience with AWS a plus")
# used to count too, which is how a company's "Tools they already use" panel
# — and its fit score — credited it with AWS, Azure, Google Cloud and Vercel
# all at once from one generic sentence. Any integration_detected row for one
# of these that did NOT come from techstack is exactly that false positive.
HOSTING_PLATFORMS_NO_LONGER_FROM_JD = {
    "Vercel", "Netlify", "Render", "Railway", "AWS", "Azure",
    "Google Cloud", "Heroku", "Fly.io",
}


def init() -> None:
    """Called on every collector run (scheduler.run_one), not just at process
    boot — schema creation and the never_target backfill are safe to repeat,
    but the orphan-reconciliation below is NOT: two collectors run
    concurrently on their own schedules, so a second collector's init() call
    firing while a first one is still legitimately mid-run would reconcile
    that live row as "orphaned" out from under it. It gets overwritten with
    the real ok/failed status moments later when that run actually finishes,
    but the stale error text sticks — false alarms on the Ops page for runs
    that never actually crashed. The `_reconciled` guard makes this run
    exactly once per process, which is the only time it's actually safe.
    """
    global _reconciled
    from .config import cfg
    with tx() as c:
        c.executescript(SCHEMA)
        # Idempotent backfill: never_target may have grown since these rows landed.
        for d in (cfg()["fit"].get("never_target") or []):
            c.execute("UPDATE account SET disqualified=? WHERE domain=? AND disqualified IS NULL",
                      ("competitor / own domain", d))
        # Idempotent backfill: purge stale evidence from pain terms that got
        # pruned as too generic (see REMOVED_PAIN_TERMS above).
        stale = []
        for r in c.execute("SELECT id, payload FROM signal WHERE kind IN "
                           "('persona_pain_match','persona_pain_post')"):
            try:
                term = json.loads(r["payload"] or "{}").get("term")
            except Exception:
                continue
            if term in REMOVED_PAIN_TERMS:
                stale.append(r["id"])
        if stale:
            c.executemany("DELETE FROM signal WHERE id=?", [(i,) for i in stale])
        # Idempotent backfill: purge owns_production_language entirely — the
        # word-boundary fix in collectors/base.py's find_terms() means every
        # existing row here may have been built from a false "sla"-inside-
        # "Slack" or "slo"-inside-"slow" match, and the detail text bundles
        # every hit into one string so there is no way to selectively fix
        # just the bad ones. Safe to wipe: it regenerates correctly, and
        # cheaply, on each account's next collector run.
        c.execute("DELETE FROM signal WHERE kind='owns_production_language'")
        # Idempotent backfill: purge competitor signals fired from a bare
        # word that has since been replaced with its actual domain.
        for kw in STALE_COMPETITOR_KEYWORDS:
            c.execute("DELETE FROM signal WHERE kind IN "
                      "('competitor_complementary','competitor_displacement') "
                      "AND (dedupe_key LIKE ? OR dedupe_key LIKE ?)",
                      (f"%:comp_c:{kw}", f"%:comp_d:{kw}"))
        # Idempotent backfill: purge integration_detected signals for hosting
        # platforms that were only ever confirmed via job-ad text (source !=
        # 'techstack') — the jd: fingerprint for these was removed since it
        # produced false positives (a JD mentioning "AWS" isn't hosting proof).
        stale_hosting = []
        for r in c.execute("SELECT id, source, payload FROM signal WHERE kind='integration_detected'"):
            if r["source"] == "techstack":
                continue
            try:
                tool = json.loads(r["payload"] or "{}").get("tool")
            except Exception:
                continue
            if tool in HOSTING_PLATFORMS_NO_LONGER_FROM_JD:
                stale_hosting.append(r["id"])
        if stale_hosting:
            c.executemany("DELETE FROM signal WHERE id=?", [(i,) for i in stale_hosting])
        # Idempotent backfill: the ICP band tightened (was 5-150, now matches
        # headcount_band's 8-60) — accounts whose real, known headcount now
        # falls outside it must stop appearing on every list page, not just
        # new ones going forward.
        f = cfg()["fit"]["disqualify"]
        for r in c.execute(
                "SELECT id, headcount FROM account WHERE disqualified IS NULL "
                "AND headcount IS NOT NULL AND (headcount < ? OR headcount > ?)",
                (f["headcount_under"], f["headcount_over"])):
            word = "below" if r["headcount"] < f["headcount_under"] else "above"
            c.execute("UPDATE account SET disqualified=? WHERE id=?",
                      (f"{r['headcount']} employees — {word} ICP band", r["id"]))
        if _reconciled:
            return
        # A row is only ever "running" while the process that started it is
        # alive — there is no legitimate way for one to survive a restart. If
        # it's still here, that process was killed mid-run (crash, redeploy,
        # manual restart) and nothing inside that dead process ever ran to
        # finalize it, so it would sit as "running" on the Ops page forever.
        # This is the one point where we can be certain any prior incarnation
        # is gone, so it is the only safe place to reconcile.
        c.execute(
            "UPDATE collector_run SET status='failed', finished_at=?, "
            "error='orphaned: process restarted mid-run' WHERE status='running'",
            (now(),))
        _reconciled = True


# ---------------------------------------------------------------- domain key

_STRIP = re.compile(r"^(https?://)?(www\.)?", re.I)


def norm_domain(raw: str | None) -> str | None:
    """The corporate domain is the primary key for entity resolution."""
    if not raw:
        return None
    d = _STRIP.sub("", raw.strip().lower()).split("/")[0].split("?")[0].split("@")[-1]
    d = d.split(":")[0].rstrip(".")
    if "." not in d or " " in d:
        return None
    # Free-mail and code hosts are never an account.
    if d in {"gmail.com", "googlemail.com", "yahoo.com", "hotmail.com", "outlook.com",
             "github.com", "linkedin.com", "google.com", "example.com"}:
        return None
    return d


# ---------------------------------------------------------------- accounts

def upsert_account(domain: str, **fields) -> int | None:
    domain = norm_domain(domain)
    if not domain:
        return None
    # Malveon itself and its direct competitors are never prospects. Enforced
    # here because every discovery path (ATS, techstack, social, EDGAR, manual
    # add) funnels through this one function.
    from .config import cfg
    blocked = domain in set(cfg()["fit"].get("never_target") or [])
    ts = now()
    with tx() as c:
        c.execute(
            "INSERT INTO account(domain, first_seen, last_seen, disqualified) VALUES(?,?,?,?) "
            "ON CONFLICT(domain) DO UPDATE SET last_seen=excluded.last_seen",
            (domain, ts, ts, "competitor / own domain" if blocked else None),
        )
        row = c.execute("SELECT id FROM account WHERE domain=?", (domain,)).fetchone()
        aid = row["id"]
        sets = {k: v for k, v in fields.items() if v is not None}
        if sets:
            # `name` is special: once a domain has one, it must never change
            # again from here. Every OTHER field prefers the newest value
            # (COALESCE(?,col)) because those legitimately get refreshed —
            # headcount, ats platform, open-role count. Name doesn't work
            # that way: a domain is one company forever, so the first name
            # wins (COALESCE(col,?)). Without this split, a discovery source
            # that misreads one noisy post — e.g. a "Fluidstack" hiring
            # thread that happened to link anthropic.com — silently renamed
            # the real Anthropic account to "Fluidstack" on the next run.
            cols = ", ".join(
                "name=COALESCE(name,?)" if k == "name" else f"{k}=COALESCE(?,{k})"
                for k in sets)
            c.execute(f"UPDATE account SET {cols} WHERE id=?", (*sets.values(), aid))
    return aid


def disqualify(account_id: int, reason: str | None) -> None:
    with tx() as c:
        c.execute("UPDATE account SET disqualified=? WHERE id=?", (reason, account_id))


# ---------------------------------------------------------------- signals

def add_signal(account_id: int, kind: str, source: str, dedupe_key: str,
               detail: str = "", url: str = "", value: float = 1.0,
               payload: dict | None = None, observed_at: str | None = None) -> bool:
    """Returns True if this was genuinely new. Safe to call repeatedly."""
    with tx() as c:
        cur = c.execute(
            "INSERT OR IGNORE INTO signal"
            "(account_id,kind,source,detail,url,value,payload,observed_at,dedupe_key)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (account_id, kind, source, detail, url, value,
             json.dumps(payload) if payload else None, observed_at or now(), dedupe_key),
        )
        return cur.rowcount > 0


def signals_for(account_id: int) -> list[sqlite3.Row]:
    return conn().execute(
        "SELECT * FROM signal WHERE account_id=? ORDER BY observed_at DESC", (account_id,)
    ).fetchall()


# ---------------------------------------------------------------- snapshots

def get_snapshot(collector: str, key: str) -> dict | None:
    row = conn().execute(
        "SELECT body FROM snapshot WHERE collector=? AND key=?", (collector, key)
    ).fetchone()
    return json.loads(row["body"]) if row else None


def put_snapshot(collector: str, key: str, body: dict) -> None:
    with tx() as c:
        c.execute(
            "INSERT INTO snapshot(collector,key,body,updated_at) VALUES(?,?,?,?) "
            "ON CONFLICT(collector,key) DO UPDATE SET body=excluded.body, updated_at=excluded.updated_at",
            (collector, key, json.dumps(body), now()),
        )
