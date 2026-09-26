"""Where Malveon's buyers actually talk — three free sources, no paid tier.

  Mastodon    The SRE/devops community largely left X for hachyderm.io and
              fosstodon.org. Public hashtag timelines need no auth and no key,
              which makes this the realistic replacement for the $200/mo X API.
  Dev.to      Free article API. Postmortem / on-call / observability writeups,
              and authors usually name their employer.
  Stargazers  Who starred Backstage, Rootly, OpsLevel or grafana/oncall in the
              last month — a live list of people shopping your category, with a
              company field on their profile.

All three feed the same reviewable intent feed as the other social sources and
reuse its persona-pain matcher, so a post describing a specific villain scores
far higher than one merely using a category word.
"""
from __future__ import annotations

import html
import logging
import re
from datetime import datetime, timedelta, timezone

from .. import db
from ..config import Env, cfg
from .base import Record, Sig, http
from .social import _account_index, _key, _persona_hit, _store, _ts

log = logging.getLogger("community")

TAGS = re.compile(r"<[^>]+>")


def _text(s: str) -> str:
    return html.unescape(TAGS.sub(" ", s or "")).strip()


def _mastodon(idx) -> list[tuple]:
    hits = []
    c = cfg()["community"]
    for inst in c["mastodon_instances"]:
        for tag in c["mastodon_tags"]:
            try:
                posts = http.json(f"https://{inst}/api/v1/timelines/tag/{tag}",
                                  params={"limit": 40})
            except Exception as exc:
                log.info("mastodon %s #%s: %s", inst, tag, exc)
                continue
            for p in posts if isinstance(posts, list) else []:
                acct = (p.get("account") or {})
                body = _text(p.get("content"))
                m = _store("mastodon", str(p.get("id")), acct.get("acct") or "",
                           p.get("url") or "", "", body, _ts(p.get("created_at")), idx)
                if m:
                    hits.append((m[0], m[1], f"Mastodon #{tag}: {body[:80]}", m[2]))
    return hits


def _devto(idx) -> list[tuple]:
    hits = []
    for tag in cfg()["community"]["devto_tags"]:
        try:
            arts = http.json("https://dev.to/api/articles",
                             params={"tag": tag, "per_page": 30})
        except Exception as exc:
            log.info("dev.to %s: %s", tag, exc)
            continue
        for a in arts if isinstance(arts, list) else []:
            body = (a.get("description") or "") + " " + " ".join(a.get("tag_list") or [])
            m = _store("devto", str(a.get("id")), (a.get("user") or {}).get("username", ""),
                       a.get("url") or "", a.get("title") or "", body,
                       _ts(a.get("published_at")), idx)
            if m:
                hits.append((m[0], m[1], f"dev.to: {(a.get('title') or '')[:80]}", m[2]))
    return hits


def _gh(path: str, accept: str = "application/vnd.github+json", **params):
    hdr = {"Accept": accept}
    if Env.GITHUB_TOKEN:
        hdr["Authorization"] = f"Bearer {Env.GITHUB_TOKEN}"
    return http.json(f"https://api.github.com{path}", params=params or None, headers=hdr)


class StargazerCollector:
    """Starring a competitor is the cheapest possible act of evaluation — and
    the GitHub profile behind it usually names an employer."""
    name = "stargazers"

    def fetch(self):
        c = cfg()["community"]
        cutoff = datetime.now(timezone.utc) - timedelta(days=c["stargazer_days"])
        # Account index by company name, since a GitHub profile gives a name.
        by_name = {}
        for r in db.conn().execute(
                "SELECT id,domain,name FROM account WHERE disqualified IS NULL"):
            for cand in (r["name"], r["domain"].split(".")[0]):
                if cand and len(cand) >= 4:
                    by_name[cand.strip().lower().lstrip("@")] = r["domain"]

        for repo in cfg()["github_watch_repos"]:
            try:
                stars = _gh(f"/repos/{repo}/stargazers",
                            accept="application/vnd.github.star+json",
                            per_page=100, page=1)
            except Exception as exc:
                # The stargazers endpoint requires auth AND rejects fine-grained
                # PATs with 403 "Resource not accessible by personal access
                # token". It needs a CLASSIC token with public_repo scope. Say
                # so once, loudly, instead of silently reporting zero records.
                if "403" in str(exc) or "401" in str(exc):
                    log.warning(
                        "stargazers needs a CLASSIC GitHub token (public_repo scope). "
                        "Fine-grained tokens cannot read this endpoint — collector will "
                        "keep returning 0 until GITHUB_TOKEN is a classic token.")
                    return
                log.info("stargazers %s: %s", repo, exc)
                continue
            for s in stars if isinstance(stars, list) else []:
                when = s.get("starred_at")
                try:
                    if when and datetime.fromisoformat(when.replace("Z", "+00:00")) < cutoff:
                        continue
                except Exception:
                    pass
                login = (s.get("user") or {}).get("login")
                if not login:
                    continue
                try:
                    u = _gh(f"/users/{login}")
                except Exception:
                    continue
                company = (u.get("company") or "").strip().lower().lstrip("@")
                dom = by_name.get(company)
                if not dom:
                    continue
                yield Record(key=f"star:{repo}:{login}", domain=dom, name=u.get("name"),
                             body={"repo": repo, "login": login, "when": when,
                                   "url": f"https://github.com/{login}"})

    def signals(self, rec: Record, prev: dict | None):
        b = rec.body
        yield Sig("competitor_evaluation", f"{rec.domain}:star:{_key(b['repo'] + b['login'])}",
                  detail=f"Someone there starred {b['repo']} — they're looking at competitors",
                  url=b["url"], observed_at=b.get("when"))


class ContactsCollector:
    """Find work emails for the accounts actually worth contacting.

    Contact discovery used to happen only when someone clicked "Find contacts"
    on one account at a time, so a freshly deployed board had no emails on it at
    all. This walks the top-ranked accounts that have none yet, newest score
    first, and pulls real addresses out of public GitHub commit history.

    Bounded per run: GitHub allows 5,000 requests/hour and each account costs
    roughly a dozen, so an unbounded sweep over 800 accounts would exhaust the
    budget and trip the breaker.
    """
    name = "contacts"

    def fetch(self):
        from .. import contacts as contacts_mod
        from .. import leads

        spec = cfg()["collectors"]["contacts"]
        ranked = leads.board(limit=5000)
        todo = [r for r in ranked if not r.get("contacts")][:spec["accounts_per_run"]]
        log.info("contacts: %d ranked accounts, %d without contacts this run",
                 len(ranked), len(todo))

        if not Env.GITHUB_TOKEN:
            log.warning("contacts: no GITHUB_TOKEN — GitHub allows 60 req/hr "
                        "unauthenticated and each account costs ~12, so this "
                        "collector will find almost nothing")
        found = orgless = 0
        for r in todo:
            res = contacts_mod.github_contacts(r["domain"])
            if not res.get("org"):
                orgless += 1
                continue
            if res.get("stored"):
                found += 1
                yield Record(key=f"contacts:{r['domain']}", domain=r["domain"],
                             body={"stored": res["stored"], "org": res.get("org"),
                                   "pattern": res.get("pattern")})
        # Without this the collector reports "ok, 0 records" and gives no clue
        # whether it found nothing or never really ran.
        log.info("contacts: %d/%d accounts yielded emails, %d had no resolvable "
                 "GitHub org", found, len(todo), orgless)

    def signals(self, rec: Record, prev: dict | None):
        b = rec.body
        # Context, not intent — having an email is not a reason to call someone.
        yield Sig("contacts_found", f"{rec.domain}:contacts:{b['stored']}",
                  detail=f"Found {b['stored']} work email addresses"
                         + (f" (pattern {b['pattern']})" if b.get("pattern") else ""),
                  url=f"https://github.com/{b['org']}" if b.get("org") else "")


SOURCES = {"mastodon": _mastodon, "devto": _devto}


class CommunityCollector:
    """One collector per source so a broken source trips only its own breaker."""

    def __init__(self, source: str):
        self.source = source
        self.name = source

    def fetch(self):
        idx = _account_index()
        for aid, domain, label, persona in SOURCES[self.source](idx):
            yield Record(key=f"{self.source}:{domain}:{_key(label)}", domain=domain,
                         body={"label": label, "persona": persona})

    def signals(self, rec: Record, prev: dict | None):
        label, persona = rec.body["label"], rec.body.get("persona")
        if persona:
            pid, term = persona
            p = cfg()["personas"][pid]
            yield Sig("persona_pain_post", f"{rec.domain}:pain:{pid}:{_key(label)}",
                      detail=f"{p['title']} pain — \"{term}\" · {label}",
                      payload={"persona": pid, "term": term, "line": p["line"]})
            return
        yield Sig("active_research", f"{rec.domain}:research:{_key(label)}", detail=label)
