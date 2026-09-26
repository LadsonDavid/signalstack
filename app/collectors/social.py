"""Intent mining from public, logged-off surfaces — all free.

Malveon's buyers argue about postmortems, on-call and Datadog bills in places
with open APIs, so this needs no paid tier and no proxies:

  Hacker News  Algolia API — unauthenticated, unlimited, and by far the highest
               signal-per-request source for this ICP
  GitHub       5,000 req/hr authenticated; who is filing issues on incident.io,
               Backstage, Rootly, OpsLevel is a live evaluation list
  Lobsters     open JSON
  Reddit       free tier, OFF by default — see the note in malveon.yaml

Two-stage by design. This collector only does the cheap part: fetch, keyword
prefilter, store, and try to attribute the post to a known account. Paid LLM
classification runs afterwards in llm.py, over the small surviving set.
"""
from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timezone
from functools import lru_cache

from .. import db
from ..config import Env, cfg
from .base import Record, Sig, http

log = logging.getLogger("social")

MIN_NAME = 6


@lru_cache(maxsize=1)
def _prefilter_re() -> re.Pattern:
    """Short terms need word boundaries. Plain substring matching let "adr" fire
    inside "quadratic"/"hadron" and flooded the feed with dependabot PRs.

    Persona pain language is included alongside the generic category terms, so
    the net catches someone saying "I found out about the blocker three days
    late" and not only someone saying "observability".
    """
    terms = list(cfg()["intent_prefilter"])
    for p in cfg()["personas"].values():
        terms += p["pain_terms"]
    parts = []
    for t in sorted(set(terms)):
        esc = re.escape(t)
        parts.append(rf"\b{esc}\b" if len(t) <= 5 else esc)
    return re.compile("|".join(parts), re.I)


@lru_cache(maxsize=1)
def _persona_res() -> list[tuple[str, re.Pattern]]:
    out = []
    for pid, p in cfg()["personas"].items():
        pat = "|".join(re.escape(t) for t in p["pain_terms"])
        out.append((pid, re.compile(pat, re.I)))
    return out


def _persona_hit(text: str) -> tuple[str, str] | None:
    """Which persona's villain is this person describing, in their own words?"""
    for pid, pat in _persona_res():
        m = pat.search(text or "")
        if m:
            return pid, m.group(0)
    return None


# Automated PRs are pure volume and carry no human intent.
BOT_AUTHOR = re.compile(r"\[bot\]$|^dependabot|^renovate|^github-actions", re.I)
BOT_TITLE = re.compile(r"^(chore\(deps\)|build\(deps\)|bump |Bump )", re.I)


def _prefilter(text: str) -> bool:
    return bool(_prefilter_re().search(text))


def _account_index() -> list[tuple[re.Pattern, int, str]]:
    """Match on the FULL domain only — never the bare label.

    Matching `domain.split('.')[0]` looked clever and was catastrophic: it made
    trigger.dev match every GitHub issue containing the word "trigger", val.town
    match "val", mux.com match "mux". A blocklist of ambiguous words is
    unwinnable, because the ambiguity is in the domain-name fashion of this
    entire market. Full-domain matching has effectively zero false positives,
    and anything it misses is exactly what the manual attach button on /feed is
    for. Precision over recall — a wrong account on the board costs outreach
    time, which is the one thing that cannot be bought back.
    """
    out = []
    for r in db.conn().execute(
            "SELECT id,domain,name FROM account WHERE disqualified IS NULL"):
        out.append((re.compile(rf"\b{re.escape(r['domain'])}\b", re.I), r["id"], r["domain"]))
        nm = (r["name"] or "").strip()
        # A multi-word or long company name is distinctive enough; a short
        # single token never is.
        if len(nm) >= MIN_NAME and nm.lower() != r["domain"].split(".")[0]:
            out.append((re.compile(rf"\b{re.escape(nm)}\b", re.I), r["id"], r["domain"]))
    return out


def _store(source: str, ext_id: str, author: str, url: str, title: str,
           body: str, created: str, idx) -> tuple[int, str] | None:
    """Store a prefiltered post. Returns (account_id, domain) if attributable."""
    if BOT_AUTHOR.search(author or "") or BOT_TITLE.search(title or ""):
        return None
    text = f"{title}\n{body}"
    if not _prefilter(text):
        return None
    aid = dom = None
    for pat, a, d in idx:
        if pat.search(text):
            aid, dom = a, d
            break
    with db.tx() as c:
        c.execute("INSERT OR IGNORE INTO intent_post"
                  "(source,ext_id,author,url,title,body,created_at,account_id) "
                  "VALUES(?,?,?,?,?,?,?,?)",
                  (source, ext_id, author, url, title, body[:8000], created, aid))
    return (aid, dom, _persona_hit(text)) if aid else None


def _ts(v) -> str:
    if isinstance(v, (int, float)):
        return datetime.fromtimestamp(v, timezone.utc).isoformat(timespec="seconds")
    return str(v or "")[:32]


# ---------------------------------------------------------------- sources

def _hn(idx) -> list[tuple[int, str, str]]:
    hits = []
    for q in cfg()["hn_queries"]:
        try:
            d = http.json("https://hn.algolia.com/api/v1/search_by_date",
                          params={"query": q, "tags": "(story,comment)", "hitsPerPage": 50})
        except Exception as exc:
            log.warning("hn %r: %s", q, exc)
            continue
        for h in d.get("hits", []):
            body = h.get("comment_text") or h.get("story_text") or ""
            title = h.get("title") or h.get("story_title") or ""
            oid = str(h.get("objectID"))
            m = _store("hn", oid, h.get("author") or "",
                       f"https://news.ycombinator.com/item?id={oid}", title, body,
                       _ts(h.get("created_at_i")), idx)
            if m:
                hits.append((m[0], m[1], f"HN: {(title or body)[:80]}", m[2]))
    return hits


def _lobsters(idx) -> list[tuple[int, str, str]]:
    hits = []
    try:
        d = http.json("https://lobste.rs/newest.json")
    except Exception as exc:
        log.warning("lobsters: %s", exc)
        return hits
    for s in d if isinstance(d, list) else []:
        m = _store("lobsters", str(s.get("short_id")), (s.get("submitter_user") or ""),
                   s.get("comments_url") or s.get("url") or "", s.get("title") or "",
                   s.get("description_plain") or s.get("description") or "",
                   _ts(s.get("created_at")), idx)
        if m:
            hits.append((m[0], m[1], f"Lobsters: {(s.get('title') or '')[:80]}", m[2]))
    return hits


def _github(idx) -> list[tuple[int, str, str]]:
    hits = []
    hdr = {"Accept": "application/vnd.github+json"}
    if Env.GITHUB_TOKEN:
        hdr["Authorization"] = f"Bearer {Env.GITHUB_TOKEN}"
    for repo in cfg()["github_watch_repos"]:
        try:
            d = http.json(f"https://api.github.com/repos/{repo}/issues",
                          params={"state": "all", "per_page": 30, "sort": "created"},
                          headers=hdr)
        except Exception as exc:
            log.warning("github %s: %s", repo, exc)
            continue
        for i in d if isinstance(d, list) else []:
            m = _store("github", f"{repo}#{i.get('number')}",
                       (i.get("user") or {}).get("login", ""), i.get("html_url") or "",
                       i.get("title") or "", i.get("body") or "",
                       _ts(i.get("created_at")), idx)
            if m:
                hits.append((m[0], m[1], f"GitHub {repo}: {(i.get('title') or '')[:70]}", m[2]))
    return hits


def _reddit(idx) -> list[tuple[int, str, str]]:
    """Free tier is NON-COMMERCIAL — gated off in malveon.yaml by default."""
    hits = []
    if not (Env.REDDIT_ID and Env.REDDIT_SECRET):
        log.info("reddit enabled but no credentials — skipping")
        return hits
    try:
        tok = http.post_json  # token via basic auth
        import base64
        auth = base64.b64encode(f"{Env.REDDIT_ID}:{Env.REDDIT_SECRET}".encode()).decode()
        import httpx
        with httpx.Client(timeout=20) as c:
            r = c.post("https://www.reddit.com/api/v1/access_token",
                       data={"grant_type": "client_credentials"},
                       headers={"Authorization": f"Basic {auth}",
                                "User-Agent": cfg()["http"]["user_agent"]})
            r.raise_for_status()
            access = r.json()["access_token"]
    except Exception as exc:
        log.warning("reddit auth: %s", exc)
        return hits
    hdr = {"Authorization": f"Bearer {access}", "User-Agent": cfg()["http"]["user_agent"]}
    for sub in cfg()["subreddits"]:
        try:
            d = http.json(f"https://oauth.reddit.com/r/{sub}/new",
                          params={"limit": 50}, headers=hdr)
        except Exception as exc:
            log.warning("reddit %s: %s", sub, exc)
            continue
        for ch in (d.get("data", {}).get("children") or []):
            p = ch.get("data", {})
            m = _store("reddit", p.get("id", ""), p.get("author", ""),
                       "https://reddit.com" + p.get("permalink", ""), p.get("title", ""),
                       p.get("selftext", ""), _ts(p.get("created_utc")), idx)
            if m:
                hits.append((m[0], m[1], f"r/{sub}: {p.get('title','')[:70]}", m[2]))
    return hits


SOURCES = {"hn": _hn, "lobsters": _lobsters, "github": _github, "reddit": _reddit}


def _key(s: str) -> str:
    """Stable across processes. Python randomises str hashing per interpreter,
    so hash() in a dedupe_key silently re-emits every signal on each restart."""
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:16]


class SocialCollector:
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
        label = rec.body["label"]
        persona = rec.body.get("persona")
        if persona:
            pid, term = persona
            p = cfg()["personas"][pid]
            # Someone describing a specific villain in their own words is worth
            # far more than someone merely using a category word.
            yield Sig("persona_pain_post", f"{rec.domain}:pain:{pid}:{_key(label)}",
                      detail=f"{p['title']} pain — \"{term}\" · {label}",
                      payload={"persona": pid, "term": term, "line": p["line"]})
            return
        # Generic category match only. The LLM later upgrades qualifying posts
        # to the heavier stated_intent / competitor_gripe kinds.
        yield Sig("active_research", f"{rec.domain}:research:{_key(label)}", detail=label)
