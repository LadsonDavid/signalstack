"""Collector protocol + the shared runner.

Every external source is an Integration Point (Nygard), so the protections live
here ONCE rather than in each collector:

  * connect + read timeout on every outbound call
  * per-source circuit breaker, persisted in SQLite so it survives restarts
  * bulkheads: a global HTTP semaphore plus a per-collector fan-out cap
  * fail-fast per record — one bad company never aborts a sweep
  * every run recorded in collector_run for transparency

A new source therefore has to implement exactly two methods and inherits all of
the above for free.
"""
from __future__ import annotations

import concurrent.futures as cf
import logging
import re
import threading
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterable, Protocol, Sequence

import httpx

from .. import db
from ..config import cfg

log = logging.getLogger("collector")

_sem: threading.Semaphore | None = None
_sem_lock = threading.Lock()


def _global_sem() -> threading.Semaphore:
    global _sem
    with _sem_lock:
        if _sem is None:
            _sem = threading.Semaphore(cfg()["http"]["global_concurrency"])
    return _sem


@dataclass
class Record:
    """One account's complete state for one collector — the diff unit."""
    key: str
    domain: str | None
    name: str | None = None
    body: dict = field(default_factory=dict)
    account_fields: dict = field(default_factory=dict)


@dataclass
class Sig:
    kind: str
    dedupe_key: str
    detail: str = ""
    url: str = ""
    value: float = 1.0
    observed_at: str | None = None
    payload: dict | None = None


class Collector(Protocol):
    name: str

    def fetch(self) -> Iterable[Record]: ...

    def signals(self, rec: Record, prev: dict | None) -> Iterable[Sig]: ...


# ---------------------------------------------------------------- HTTP

class Http:
    """Every outbound call goes through here so nothing can hang a sweep."""

    def __init__(self) -> None:
        h = cfg()["http"]
        self.timeout = httpx.Timeout(connect=h["connect_timeout"], read=h["read_timeout"],
                                     write=h["read_timeout"], pool=h["read_timeout"])
        self.headers = {"User-Agent": h["user_agent"], "Accept": "*/*"}

    def get(self, url: str, **kw) -> httpx.Response:
        headers = {**self.headers, **kw.pop("headers", {})}
        with _global_sem():
            with httpx.Client(timeout=self.timeout, follow_redirects=True,
                              verify=kw.pop("verify", True)) as c:
                return c.get(url, headers=headers, **kw)

    def json(self, url: str, **kw):
        r = self.get(url, **kw)
        r.raise_for_status()
        return r.json()

    def post_json(self, url: str, payload: dict, headers: dict | None = None):
        with _global_sem():
            with httpx.Client(timeout=self.timeout, follow_redirects=True) as c:
                r = c.post(url, json=payload, headers={**self.headers, **(headers or {})})
                r.raise_for_status()
                return r.json()


http = Http()


def find_terms(terms: Sequence[str], blob: str) -> list[str]:
    """Which of `terms` actually appear in `blob` — the terms Malveon actually
    fired on, quotable back to the user, not just "matched something."

    Short terms (<=5 chars) get a word boundary; plain substring matching let
    "sla" fire inside "Slack" and "slo" fire inside "slow" — every company
    that merely mentioned Slack in a job post was credited with "their job
    ads say: sla". Longer terms (including multi-word phrases like "root
    cause analysis") are left as plain substrings — they can't collide with
    an unrelated word by accident the way a bare 3-letter term can.
    """
    out = []
    for t in terms:
        pat = re.escape(t)
        if len(t) <= 5:
            pat = rf"\b{pat}\b"
        if re.search(pat, blob, re.I):
            out.append(t)
    return out


def fan_out(items: Sequence, worker: Callable, concurrency: int) -> list:
    """Bulkhead: bounded parallel fan-out. A failing item yields None, never raises."""
    out: list = []
    if not items:
        return out

    def safe(it):
        try:
            return worker(it)
        except Exception as exc:  # one bad company must not poison the batch
            log.warning("item failed %r: %s", it, exc)
            return None

    with cf.ThreadPoolExecutor(max_workers=max(1, concurrency)) as ex:
        out.extend(ex.map(safe, items))
    return out


# ---------------------------------------------------------------- breaker

def _breaker_open(name: str) -> bool:
    row = db.conn().execute("SELECT opened_until FROM breaker WHERE collector=?", (name,)).fetchone()
    if not row or not row["opened_until"]:
        return False
    return datetime.fromisoformat(row["opened_until"]) > datetime.now(timezone.utc)


def _breaker_trip(name: str) -> None:
    b = cfg()["breaker"]
    with db.tx() as c:
        c.execute("INSERT INTO breaker(collector,failures) VALUES(?,0) "
                  "ON CONFLICT(collector) DO NOTHING", (name,))
        c.execute("UPDATE breaker SET failures=failures+1 WHERE collector=?", (name,))
        n = c.execute("SELECT failures FROM breaker WHERE collector=?", (name,)).fetchone()["failures"]
        if n >= b["failure_threshold"]:
            until = datetime.now(timezone.utc) + timedelta(minutes=b["open_minutes"])
            c.execute("UPDATE breaker SET opened_until=? WHERE collector=?", (until.isoformat(), name))
            log.error("circuit breaker OPEN for %s until %s", name, until)


def _breaker_reset(name: str) -> None:
    with db.tx() as c:
        c.execute("INSERT INTO breaker(collector,failures,opened_until) VALUES(?,0,NULL) "
                  "ON CONFLICT(collector) DO UPDATE SET failures=0, opened_until=NULL", (name,))


# ---------------------------------------------------------------- runner

def run_collector(col: Collector) -> dict:
    """Run one collector end to end. Never raises."""
    name = col.name
    if _breaker_open(name):
        log.warning("skipping %s — breaker open", name)
        with db.tx() as c:
            c.execute("INSERT INTO collector_run(collector,started_at,finished_at,status) "
                      "VALUES(?,?,?,?)", (name, db.now(), db.now(), "skipped_open_breaker"))
        return {"collector": name, "status": "skipped_open_breaker"}

    started = db.now()
    with db.tx() as c:
        cur = c.execute("INSERT INTO collector_run(collector,started_at,status) VALUES(?,?,?)",
                        (name, started, "running"))
        run_id = cur.lastrowid

    n_rec = n_sig = 0
    try:
        for rec in col.fetch():
            try:
                n_rec += 1
                aid = None
                if rec.domain:
                    aid = db.upsert_account(rec.domain, name=rec.name, **rec.account_fields)
                if aid is None:
                    continue
                prev = db.get_snapshot(name, rec.key)
                for s in col.signals(rec, prev):
                    if db.add_signal(aid, s.kind, name, s.dedupe_key, s.detail, s.url,
                                     s.value, s.payload, s.observed_at):
                        n_sig += 1
                db.put_snapshot(name, rec.key, rec.body)
            except Exception:
                log.warning("record failed in %s: %s", name, traceback.format_exc(limit=3))
    except Exception as exc:
        _breaker_trip(name)
        with db.tx() as c:
            c.execute("UPDATE collector_run SET finished_at=?,status=?,records=?,signals=?,error=? "
                      "WHERE id=?", (db.now(), "failed", n_rec, n_sig, str(exc)[:500], run_id))
        log.error("collector %s FAILED: %s", name, exc)
        return {"collector": name, "status": "failed", "error": str(exc)}

    _breaker_reset(name)
    with db.tx() as c:
        c.execute("UPDATE collector_run SET finished_at=?,status=?,records=?,signals=? WHERE id=?",
                  (db.now(), "ok", n_rec, n_sig, run_id))
    log.info("%s ok records=%d new_signals=%d", name, n_rec, n_sig)
    return {"collector": name, "status": "ok", "records": n_rec, "signals": n_sig}
