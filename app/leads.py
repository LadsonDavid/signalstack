"""The three lead types are three QUERIES over one signal log, not three pipelines.

The vendors in the research build these as separate systems because separate
teams built them years apart. Over an append-only signal log they are views:

  Company Lead — a trigger fired inside the window (funding / first SRE / first
                 EM / req surge / new status page)
  Intent Lead  — they said something in public that reads as buying intent, or
                 they are visibly researching the category
  Cold Lead    — fits the ICP, zero live intent. Future pipeline, not today's.
"""
from __future__ import annotations

from collections import defaultdict

from . import db, scoring
from .config import cfg


def _types(rows, sc: dict) -> list[str]:
    out = []
    win = cfg()["thresholds"]["trigger_window_days"]
    if scoring.recent_trigger(rows, win):
        out.append("company")
    text_hits = [r for r in rows if r["kind"] in scoring.intent_kinds()]
    strong = {"stated_intent", "site_visit_pricing", "persona_pain_post", "competitor_evaluation"}
    if any(r["kind"] in strong for r in text_hits) or len(text_hits) >= 2:
        out.append("intent")
    if sc["intent"] == 0 and sc["fit"] >= cfg()["thresholds"]["mql"]:
        out.append("cold")
    return out


def board(lead_type: str | None = None, watchlist: bool = False,
          q: str | None = None, limit: int = 300) -> list[dict]:
    c = db.conn()

    sql = "SELECT * FROM account WHERE disqualified IS NULL"
    params: list = []
    if watchlist:
        sql += " AND watchlisted=1"
    if q:
        sql += " AND (domain LIKE ? OR name LIKE ?)"
        params += [f"%{q}%", f"%{q}%"]
    accounts = {r["id"]: dict(r) for r in c.execute(sql, params)}
    if not accounts:
        return []

    rows_by = defaultdict(list)
    for r in c.execute("SELECT * FROM signal WHERE account_id IN "
                       f"({','.join('?' * len(accounts))})", tuple(accounts)):
        rows_by[r["account_id"]].append(r)

    # Show the decision maker first, not whoever sorts alphabetically. A
    # confirmed address for a VP Eng beats a guessed one for a junior dev.
    from .contacts import SENIOR

    def rank(x: dict) -> tuple:
        blob = f"{x.get('title') or ''} {x.get('name') or ''}".lower()
        seniority = next((i for i, t in enumerate(SENIOR) if t in blob), 99)
        confirmed = 0 if x.get("status") in ("valid", "pattern_confirmed") else 1
        return (confirmed, seniority, x.get("email") or "")

    # A known-bad address ("this address doesn't exist, don't send") must
    # never fill the "who to email" slot — that's worse than showing no
    # contact at all, since it looks like a real lead to act on.
    contacts = defaultdict(list)
    for r in c.execute("SELECT * FROM contact WHERE account_id IN "
                       f"({','.join('?' * len(accounts))}) AND status != 'invalid'",
                       tuple(accounts)):
        contacts[r["account_id"]].append(dict(r))
    for v in contacts.values():
        v.sort(key=rank)

    out = []
    for aid, a in accounts.items():
        rows = rows_by.get(aid, [])
        sc = scoring.score_rows(rows, a.get("headcount"))
        types = _types(rows, sc)
        if lead_type and lead_type not in types:
            continue
        trig = scoring.recent_trigger(rows, cfg()["thresholds"]["trigger_window_days"])
        out.append({
            **a, **sc,
            "types": types,
            "tier": scoring.tier(sc["score"]),
            "why": scoring.explain(sc, skip=(trig["detail"] if trig else None)),
            "trigger": (trig["detail"] if trig else ""),
            "contacts": contacts.get(aid, []),
            "n_signals": len(rows),
        })

    # Cold leads have score 0 by definition, so rank them on fit instead.
    key = (lambda x: (x["fit"], x["n_signals"])) if lead_type == "cold" \
        else (lambda x: (x["score"], x["fit"]))
    out.sort(key=key, reverse=True)
    return out[:limit]


# Someone actually said something, in their own words — a real quote, not a
# tool detected on their site or an outage picked up automatically. Outages
# alone outnumber every one of these kinds combined 4 to 1 on the real board,
# which is exactly why this needs its own page instead of competing for the
# top-3 "why" slot on every company's row.
PAIN_KINDS = ("persona_pain_post", "persona_pain_match", "active_research",
             "competitor_gripe", "stated_intent", "competitor_evaluation")

HOSTING_PLATFORMS = ("Vercel", "Netlify", "Render", "Railway", "AWS", "Azure",
                    "Google Cloud", "Heroku", "Fly.io")


def pain_points(limit: int = 300) -> list[dict]:
    """Companies with at least one real human-voice signal, with the actual
    quotes attached and — when detectable — where they're hosted."""
    c = db.conn()
    ph = ",".join("?" * len(PAIN_KINDS))
    ids = [r["account_id"] for r in c.execute(
        f"SELECT DISTINCT account_id FROM signal WHERE kind IN ({ph})", PAIN_KINDS)]
    if not ids:
        return []
    aph = ",".join("?" * len(ids))
    accounts = {r["id"]: dict(r) for r in c.execute(
        f"SELECT * FROM account WHERE id IN ({aph}) AND disqualified IS NULL", ids)}

    out = []
    for aid, a in accounts.items():
        rows = db.signals_for(aid)
        sc = scoring.score_rows(rows, a.get("headcount"))
        out.append({
            **a, **sc,
            "quotes": [dict(r) for r in rows if r["kind"] in PAIN_KINDS],
            "hosting": _hosting_for(rows),
        })
    out.sort(key=lambda x: (x["score"], x["fit"]), reverse=True)
    return out[:limit]


def _hosting_for(rows) -> list[str]:
    """Only techstack's own probe counts — it's the one collector that reads
    a real HTTP header or DNS record their server actually sent. Every other
    collector's `integration_detected` for the same tool name came from
    scanning JOB-AD TEXT for a word like "AWS", which is how a company ended
    up looking "hosted on" four different clouds it never touched."""
    import json
    out, seen = [], set()
    for r in rows:
        if r["kind"] != "integration_detected" or r["source"] != "techstack":
            continue
        try:
            tool = json.loads(r["payload"] or "{}").get("tool")
        except Exception:
            tool = None
        if tool in HOSTING_PLATFORMS and tool not in seen:
            seen.add(tool)
            out.append(tool)
    return out


def detail(domain: str) -> dict | None:
    c = db.conn()
    a = c.execute("SELECT * FROM account WHERE domain=?", (domain,)).fetchone()
    if not a:
        return None
    a = dict(a)
    rows = db.signals_for(a["id"])
    sc = scoring.score_rows(rows, a.get("headcount"))
    runs = c.execute("SELECT * FROM collector_run ORDER BY started_at DESC LIMIT 12").fetchall()
    return {
        **a, **sc,
        "types": _types(rows, sc),
        "tier": scoring.tier(sc["score"]),
        "signals": [dict(r) for r in rows],
        "contacts": [dict(r) for r in c.execute(
            "SELECT * FROM contact WHERE account_id=? ORDER BY id", (a["id"],))],
        "runs": [dict(r) for r in runs],
    }


def stats() -> dict:
    c = db.conn()
    one = lambda s, *p: c.execute(s, p).fetchone()[0]
    return {
        "accounts": one("SELECT COUNT(*) FROM account WHERE disqualified IS NULL"),
        "disqualified": one("SELECT COUNT(*) FROM account WHERE disqualified IS NOT NULL"),
        "signals": one("SELECT COUNT(*) FROM signal"),
        "contacts": one("SELECT COUNT(*) FROM contact"),
        "verified": one("SELECT COUNT(*) FROM contact WHERE status='valid'"),
        "watchlist": one("SELECT COUNT(*) FROM account WHERE watchlisted=1"),
    }
