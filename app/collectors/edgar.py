"""SEC EDGAR Form D — funding triggers, free and authoritative.

Form D is the earliest public paper trail of a US private raise, usually ahead
of any press coverage. The daily index is a flat file; the trap is that the
majority of Form D volume is pooled investment funds raising their OWN capital
(hedge/PE/VC funds), which are not prospects. Those are filtered on the 3(c)
exemption codes and the "Pooled Investment Fund" industry group.

Attribution note: Form D carries no website field, so a filing is matched to an
account by name against the universe you already discovered. Unmatched filings
are counted and logged rather than used to invent accounts with guessed domains
— a funding signal you cannot act on is not worth polluting the universe for.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

from .. import db
from ..config import cfg
from .base import Record, Sig, http

log = logging.getLogger("edgar")

POOLED = re.compile(r"pooled investment fund|hedge fund|private equity fund|venture capital fund", re.I)
EXEMPT_3C = re.compile(r"3C\.?7|3C\.?1|Section 3\(c\)", re.I)
SEED_A_MAX = 60_000_000     # above this it is not the 10-150 engineer band


def _norm(s: str) -> str:
    s = re.sub(r"[^a-z0-9 ]", " ", (s or "").lower())
    s = re.sub(r"\b(inc|llc|ltd|corp|corporation|co|holdings|group|technologies|technology|labs|software|the)\b", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _index_urls(days: int) -> list[str]:
    out = []
    today = datetime.now(timezone.utc).date()
    for i in range(days):
        d = today - timedelta(days=i)
        if d.weekday() >= 5:
            continue
        q = (d.month - 1) // 3 + 1
        out.append(f"https://www.sec.gov/Archives/edgar/daily-index/{d.year}/QTR{q}/form.{d:%Y%m%d}.idx")
    return out


def _filings(days: int) -> list[dict]:
    """Parse the daily form index for Form D entries."""
    out = []
    for url in _index_urls(days):
        try:
            r = http.get(url)
            if r.status_code >= 400:
                continue
        except Exception as exc:
            log.warning("edgar index %s: %s", url, exc)
            continue
        for line in r.text.splitlines():
            if not line.startswith(("D ", "D/A")):
                continue
            parts = [p.strip() for p in re.split(r"\s{2,}", line)]
            if len(parts) < 5:
                continue
            form, company, cik, date, path = parts[0], parts[1], parts[2], parts[3], parts[-1]
            if form not in ("D", "D/A"):
                continue
            out.append({"form": form, "company": company, "cik": cik,
                        "date": date, "path": path})
    return out


def _primary_doc(path: str) -> str:
    """The .txt dissemination file contains the Form D XML inline."""
    try:
        r = http.get(f"https://www.sec.gov/Archives/{path.lstrip('/')}")
        return r.text if r.status_code < 400 else ""
    except Exception:
        return ""


def _amount(xml: str) -> float:
    m = re.search(r"<totalAmountSold>([\d.]+)</totalAmountSold>", xml)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            pass
    m = re.search(r"<totalOfferingAmount>([\d.]+)</totalOfferingAmount>", xml)
    try:
        return float(m.group(1)) if m else 0.0
    except ValueError:
        return 0.0


class EdgarCollector:
    name = "edgar"

    def __init__(self, days: int = 3):
        self.days = days
        self.unmatched = 0

    def fetch(self):
        rows = db.conn().execute(
            "SELECT id,domain,name FROM account WHERE disqualified IS NULL").fetchall()
        by_name: dict[str, dict] = {}
        for r in rows:
            for cand in (r["name"], r["domain"].split(".")[0]):
                n = _norm(cand or "")
                if len(n) >= 4:
                    by_name.setdefault(n, dict(r))

        for f in _filings(self.days):
            key = _norm(f["company"])
            acct = by_name.get(key)
            if not acct:
                self.unmatched += 1
                continue
            xml = _primary_doc(f["path"])
            if POOLED.search(xml) or EXEMPT_3C.search(xml):
                continue                                   # a fund raising its own capital
            amt = _amount(xml)
            if amt <= 0 or amt > SEED_A_MAX:
                continue
            yield Record(
                key=f"{f['cik']}:{f['date']}", domain=acct["domain"], name=acct["name"],
                body={"amount": amt, "date": f["date"], "form": f["form"],
                      "cik": f["cik"], "company": f["company"]},
            )
        if self.unmatched:
            log.info("edgar: %d Form D filings had no match in the universe", self.unmatched)

    def signals(self, rec: Record, prev: dict | None):
        b = rec.body
        d = b["date"]
        when = f"{d[:4]}-{d[4:6]}-{d[6:8]}" if len(d) == 8 else None
        yield Sig("funding_seed_a", f"{rec.domain}:formd:{b['cik']}:{d}",
                  detail=f"Just raised ${b['amount']:,.0f} — new budget to spend",
                  url=f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={b['cik']}",
                  value=b["amount"], observed_at=when, payload=b)
