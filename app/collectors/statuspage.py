"""Status-page incident feeds — the best TIMING signal available.

Everything else in this system measures *fit*: does this company look like a
Malveon customer. This measures *now*: is the Tech Lead villain — 45 minutes of
tab-switching to find a root cause — happening to them this week.

A company that shipped a major incident five days ago is living the problem
Malviont solves, today. That is a different and much better reason to send an
email than "you use Jira".

Free and structured: Atlassian-hosted status pages expose incident history at
/api/v2/incidents.json with no auth. Verified live against render.com,
honeycomb.io and supabase.com. Non-Atlassian providers (Instatus, BetterStack —
Linear and PostHog use these) serve HTML at the same path, so the provider is
detected by content-type and anything else is skipped rather than mis-parsed.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from .. import db
from ..config import cfg
from .base import Record, Sig, fan_out, http

log = logging.getLogger("statuspage")

MAJOR = {"major", "critical"}
STREAK_DAYS = 30
STREAK_MIN = 3


def _age_days(ts: str | None) -> float | None:
    if not ts:
        return None
    try:
        d = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - d).total_seconds() / 86400


def _ago(days: float) -> str:
    """"3 days ago" reads faster than a date when you're scanning a list."""
    d = int(days)
    if d <= 0:
        return "today"
    if d == 1:
        return "yesterday"
    if d < 14:
        return f"{d} days ago"
    if d < 60:
        return f"{d // 7} weeks ago"
    return f"{d // 30} months ago"


def fetch_incidents(status_host: str) -> list[dict] | None:
    """Returns incidents, or None if this is not an Atlassian status page."""
    try:
        r = http.get(f"https://{status_host}/api/v2/incidents.json")
    except Exception as exc:
        log.info("statuspage %s: %s", status_host, exc)
        return None
    if r.status_code >= 400:
        return None
    # Instatus/BetterStack answer the same path with an HTML app shell. Parsing
    # that as JSON would either explode or silently yield nothing.
    if "application/json" not in r.headers.get("content-type", ""):
        return None
    try:
        return (r.json() or {}).get("incidents") or []
    except Exception:
        return None


class StatuspageCollector:
    name = "statuspage"

    def __init__(self, only_domain: str | None = None):
        self.only = only_domain

    def _targets(self) -> list[dict]:
        """Accounts already known to publish a status page."""
        q = ("SELECT DISTINCT a.domain, a.name, s.url FROM account a "
             "JOIN signal s ON s.account_id = a.id AND s.kind = 'status_page' "
             "WHERE a.disqualified IS NULL")
        params: tuple = ()
        if self.only:
            q += " AND a.domain = ?"
            params = (self.only,)
        return [dict(r) for r in db.conn().execute(q, params)]

    def fetch(self):
        targets = self._targets()
        log.info("statuspage: %d accounts with a known status page", len(targets))

        def one(t: dict) -> Record | None:
            host = (t.get("url") or "").replace("https://", "").replace("http://", "").strip("/")
            if not host or "/" in host:
                host = f"status.{t['domain']}"
            incidents = fetch_incidents(host)
            if incidents is None:
                return None
            keep = []
            for i in incidents[:40]:
                age = _age_days(i.get("created_at"))
                if age is None or age > 90:
                    continue
                keep.append({"id": i.get("id"), "name": (i.get("name") or "")[:140],
                             "impact": (i.get("impact") or "none").lower(),
                             "status": i.get("status"), "created_at": i.get("created_at"),
                             "url": i.get("shortlink") or f"https://{host}"})
            return Record(key=t["domain"], domain=t["domain"], name=t.get("name"),
                          body={"host": host, "incidents": keep})

        for rec in fan_out(targets, one, cfg()["collectors"]["statuspage"]["concurrency"]):
            if rec:
                yield rec

    def signals(self, rec: Record, prev: dict | None):
        incidents = rec.body["incidents"]
        dom = rec.domain
        if not incidents:
            return

        recent_30 = [i for i in incidents if (_age_days(i["created_at"]) or 999) <= STREAK_DAYS]

        # detail= is what a human reads on the board, so it says what happened
        # in their words, not the provider's severity enum ("major incident:").
        for i in incidents:
            age = _age_days(i["created_at"]) or 999
            if i["impact"] in MAJOR and age <= 30:
                yield Sig("major_incident", f"{dom}:incident:{i['id']}",
                          detail=f"Big outage {_ago(age)}: {i['name']}",
                          url=i["url"], observed_at=i["created_at"],
                          payload={"impact": i["impact"]})
            elif age <= 14:
                yield Sig("recent_incident", f"{dom}:incident:{i['id']}",
                          detail=f"Outage {_ago(age)}: {i['name']}",
                          url=i["url"], observed_at=i["created_at"],
                          payload={"impact": i["impact"]})

        # Repeated incidents in a month is a team under sustained pressure —
        # a better conversation opener than any single outage.
        if len(recent_30) >= STREAK_MIN:
            yield Sig("incident_streak", f"{dom}:streak:{db.now()[:10]}",
                      detail=f"{len(recent_30)} outages in the past month — "
                             "their team is under real pressure right now",
                      url=f"https://{rec.body['host']}", value=len(recent_30))
