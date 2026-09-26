"""ATS collector — the workhorse.

One fetch of a company's public job board yields FOUR things at once:

  1. discovery        — the account itself enters your universe
  2. hiring triggers  — first SRE / first EM / engineering req surge
  3. technographics   — Jira, Linear, Notion, PagerDuty, Datadog, K8s... named in
                        the job text. This is the only way to see tools a web
                        crawler structurally cannot (they aren't on the website).
  4. prod ownership   — "on-call", "postmortem", "SLO" in the JD proves they run
                        production themselves, which is Malveon's whole premise.

All five boards are public, unauthenticated JSON. No proxies, no credentials,
no scraping of logged-in surfaces — the lowest-risk source in the stack.
"""
from __future__ import annotations

import html
import logging
import re
from datetime import datetime, timezone

from .. import db
from ..config import cfg
from .base import Record, Sig, fan_out, find_terms, http

log = logging.getLogger("ats")

TAGS = re.compile(r"<[^>]+>")


def _text(*vals) -> str:
    out = []
    for v in vals:
        if isinstance(v, str) and v:
            out.append(TAGS.sub(" ", html.unescape(v)))
        elif isinstance(v, list):
            out.append(" ".join(str(x) for x in v))
    return " ".join(out)


def _iso(v) -> str | None:
    if not v:
        return None
    if isinstance(v, (int, float)):  # Lever uses epoch millis
        try:
            return datetime.fromtimestamp(v / 1000, timezone.utc).isoformat(timespec="seconds")
        except Exception:
            return None
    return str(v)[:32]


# ---------------------------------------------------------------- adapters
# Each returns [{id, title, url, text, posted, dept}]. Verified live against
# Greenhouse (9/9 tokens), Lever and Ashby; Workable/SmartRecruiters shapes are
# documented but should be re-checked with `--smoke` when you first seed one.

def _greenhouse(tok: str) -> list[dict]:
    d = http.json(f"https://boards-api.greenhouse.io/v1/boards/{tok}/jobs?content=true")
    return [{
        "id": str(j.get("id")), "title": j.get("title") or "",
        "url": j.get("absolute_url") or "",
        "text": _text(j.get("content"), j.get("title")),
        "posted": _iso(j.get("updated_at") or j.get("first_published")),
        "dept": _text([x.get("name") for x in (j.get("departments") or []) if x]),
    } for j in d.get("jobs", [])]


def _lever(tok: str) -> list[dict]:
    d = http.json(f"https://api.lever.co/v0/postings/{tok}?mode=json")
    out = []
    for j in d if isinstance(d, list) else []:
        cat = j.get("categories") or {}
        lists = " ".join(_text(x.get("text"), x.get("content")) for x in (j.get("lists") or []))
        out.append({
            "id": str(j.get("id")), "title": j.get("text") or "",
            "url": j.get("hostedUrl") or "",
            "text": _text(j.get("descriptionPlain") or j.get("description"), j.get("text")) + " " + lists,
            "posted": _iso(j.get("createdAt")),
            "dept": _text(cat.get("team"), cat.get("department")),
        })
    return out


def _ashby(tok: str) -> list[dict]:
    d = http.json(f"https://api.ashbyhq.com/posting-api/job-board/{tok}?includeCompensation=true")
    return [{
        "id": str(j.get("id")), "title": j.get("title") or "",
        "url": j.get("jobUrl") or j.get("applyUrl") or "",
        "text": _text(j.get("descriptionPlain") or j.get("descriptionHtml") or j.get("description"),
                      j.get("title")),
        "posted": _iso(j.get("publishedAt") or j.get("updatedAt")),
        "dept": _text(j.get("department"), j.get("team")),
    } for j in d.get("jobs", [])]


def _workable(tok: str) -> list[dict]:
    d = http.json(f"https://apply.workable.com/api/v1/widget/accounts/{tok}?details=true")
    return [{
        "id": str(j.get("shortcode") or j.get("id")), "title": j.get("title") or "",
        "url": j.get("url") or j.get("application_url") or "",
        "text": _text(j.get("description"), j.get("requirements"), j.get("title")),
        "posted": _iso(j.get("published_on") or j.get("created_at")),
        "dept": _text(j.get("department")),
    } for j in d.get("jobs", [])]


def _smartrecruiters(tok: str) -> list[dict]:
    d = http.json(f"https://api.smartrecruiters.com/v1/companies/{tok}/postings?limit=100")
    return [{
        "id": str(j.get("id")), "title": j.get("name") or "",
        "url": (j.get("ref") or ""),
        # The list endpoint carries no description; titles still drive triggers.
        "text": _text(j.get("name"), (j.get("department") or {}).get("label")),
        "posted": _iso(j.get("releasedDate")),
        "dept": _text((j.get("department") or {}).get("label")),
    } for j in d.get("content", [])]


ADAPTERS = {
    "greenhouse": _greenhouse, "lever": _lever, "ashby": _ashby,
    "workable": _workable, "smartrecruiters": _smartrecruiters,
}


# ---------------------------------------------------------------- discovery

def _candidates(domain: str) -> list[str]:
    base = domain.split(".")[0]
    seen, out = set(), []
    for c in (base, base.replace("-", ""), base.replace("-", "_"), domain.replace(".", "")):
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out


def discover(domain: str) -> tuple[str, str] | None:
    """Find which ATS a domain uses. Returns (platform, token) or None."""
    for plat, fn in ADAPTERS.items():
        for tok in _candidates(domain):
            try:
                jobs = fn(tok)
            except Exception:
                continue
            if jobs:
                log.info("discovered %s -> %s:%s (%d jobs)", domain, plat, tok, len(jobs))
                return plat, tok
    return None


# ---------------------------------------------------------------- collector

class AtsCollector:
    name = "ats"

    def __init__(self, only_domain: str | None = None):
        self.only = only_domain

    def _targets(self) -> list[dict]:
        """Known boards always refresh; discovery of new ones is rate-limited.

        Discovery costs up to 15 probes per unknown domain. With a universe in
        the hundreds that is >10k requests and a sweep that never finishes, so
        undiscovered accounts are worked through a chunk at a time — oldest
        first, so every account eventually gets its turn.
        """
        c = db.conn()
        if self.only:
            return [dict(r) for r in c.execute(
                "SELECT id,domain,name,ats,ats_token FROM account WHERE domain=?",
                (self.only,))]

        known = [dict(r) for r in c.execute(
            "SELECT id,domain,name,ats,ats_token FROM account "
            "WHERE disqualified IS NULL AND ats IS NOT NULL AND ats != 'none'")]
        cap = cfg()["collectors"]["ats"].get("discover_per_run", 60)
        fresh = [dict(r) for r in c.execute(
            "SELECT id,domain,name,ats,ats_token FROM account "
            "WHERE disqualified IS NULL AND ats IS NULL ORDER BY first_seen LIMIT ?", (cap,))]
        log.info("ats: %d known boards + %d discovery attempts", len(known), len(fresh))
        return known + fresh

    def fetch(self):
        targets = self._targets()
        conc = cfg()["collectors"]["ats"]["concurrency"]

        def one(t: dict) -> Record | None:
            plat, tok = t.get("ats"), t.get("ats_token")
            if plat == "none":
                return None                      # known to have no public board
            if not plat or not tok:
                found = discover(t["domain"])
                if not found:
                    db.upsert_account(t["domain"], ats="none")
                    return None
                plat, tok = found
            jobs = ADAPTERS[plat](tok)
            eng_terms = cfg()["roles"]["eng"]
            eng = [j for j in jobs if any(e in j["title"].lower() for e in eng_terms)]
            return Record(
                key=t["domain"], domain=t["domain"], name=t.get("name"),
                body={"jobs": jobs, "eng": len(eng), "total": len(jobs)},
                account_fields={"ats": plat, "ats_token": tok, "eng_open_reqs": len(eng)},
            )

        for rec in fan_out(targets, one, conc):
            if rec:
                yield rec

    def signals(self, rec: Record, prev: dict | None):
        c = cfg()
        jobs = rec.body["jobs"]
        blob = " ".join(j["text"] + " " + j["title"] + " " + j["dept"] for j in jobs).lower()
        dom = rec.domain

        # --- hard disqualifiers -------------------------------------------
        name_blob = f"{rec.name or ''} {dom}".lower()
        for kw in c["fit"]["disqualify"]["keywords"]:
            if kw in name_blob:
                aid = db.upsert_account(dom)
                if aid:
                    db.disqualify(aid, f"matched disqualifier '{kw.strip()}'")
                return

        # --- fit: technographics from job text ----------------------------
        for tool, fp in c["fingerprints"].items():
            for term in fp.get("jd", []):
                if term in blob:
                    yield Sig("integration_detected", f"{dom}:integration:{tool}",
                              detail=f"{tool} named in job posts",
                              payload={"tool": tool, "line": fp.get("line")})
                    break

        # --- context: adjacent stack, not a Malveon integration -----------
        # `stack_evidence` is absent from the fit/intent config on purpose, so
        # it scores zero and only appears as context in the timeline.
        for tool, terms in (c.get("stack_evidence") or {}).items():
            if any(t in blob for t in terms):
                yield Sig("stack_evidence", f"{dom}:stack:{tool}",
                          detail=f"{tool} in job posts", payload={"tool": tool})

        # --- fit: they actually own production ----------------------------
        hits = find_terms(c["owns_production_terms"], blob)
        if hits:
            yield Sig("owns_production_language", f"{dom}:owns_prod",
                      detail="their job ads say: " + ", ".join(f"“{h}”" for h in hits[:4]))

        # --- fit: their own job ads describe a persona's pain --------------
        # A company writing "you'll reduce our incident triage time" into a JD
        # has just told you which villain it is living with.
        for pid, p in c["personas"].items():
            found = find_terms(p["pain_terms"], blob)
            hit = found[0] if found else None
            if hit:
                yield Sig("persona_pain_match", f"{dom}:persona:{pid}",
                          detail=f"Their job ad describes the {p['title']} problem: \"{hit}\"",
                          payload={"persona": pid, "term": hit, "line": p["line"]})

        # --- fit: competitor posture --------------------------------------
        # A vendor always names itself in its own job posts; honeycomb.io must
        # not be credited with "already runs honeycomb".
        own = dom.split(".")[0]

        def _theirs(kw: str) -> bool:
            return own not in kw and kw.split(".")[0] != own

        for kw in find_terms(c["competitors_complementary"], blob):
            if _theirs(kw):
                yield Sig("competitor_complementary", f"{dom}:comp_c:{kw}",
                          detail=f"Already pays for {kw}, so they buy tools like this")
        for kw in find_terms(c["competitors_displacement"], blob):
            if _theirs(kw):
                yield Sig("competitor_displacement", f"{dom}:comp_d:{kw}",
                          detail=f"Uses {kw}, a direct competitor — worth a switching conversation")

        # --- intent: role triggers ----------------------------------------
        prev_titles = " ".join(j["title"].lower() for j in (prev or {}).get("jobs", []))
        for kind, key in (("first_sre_hire", "sre"), ("first_em_hire", "em"),
                         ("first_release_hire", "release"), ("first_security_hire", "security")):
            terms = c["roles"][key]
            new = [j for j in jobs if any(t in j["title"].lower() for t in terms)]
            if not new:
                continue
            had_before = prev is not None and any(t in prev_titles for t in terms)
            if had_before:
                continue          # only the FIRST one is the buying signal
            j = new[0]
            yield Sig(kind, f"{dom}:{kind}:{j['id']}",
                      detail=f"hiring {j['title']}" + ("" if prev else " (first seen)"),
                      url=j["url"], observed_at=j["posted"])

        # --- intent: engineering req surge --------------------------------
        if prev:
            before, after = prev.get("eng", 0), rec.body["eng"]
            if before >= 2 and after > before * (1 + c["eng_req_surge_pct"] / 100):
                bucket = db.now()[:10]
                yield Sig("eng_req_surge", f"{dom}:surge:{bucket}",
                          detail=f"Ramping up hiring — engineering roles went from {before} to {after}",
                          value=after - before)
