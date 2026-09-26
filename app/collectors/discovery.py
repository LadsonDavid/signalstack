"""Universe discovery — where volume actually comes from.

The account list was 41 because seeds.txt was 46 hand-typed lines. That is a
list, not a discovery engine. These collectors are *generative*: they surface
companies nobody typed.

  hn_hiring  "Ask HN: Who is hiring?" — 400-1,200 top-level comments per monthly
             thread, each a company posting its own URL and, usually, its stack.
             Discovery + technographics + hiring intent from one free request.
  yc         The Y Combinator directory — thousands of companies that ARE this
             ICP by construction: software, seed-to-B, they own production.
  show_hn    "Show HN" launch posts. Noisier than hn_hiring (weekend projects,
             not just companies), but free and zero new dependency — same API.
  wwr        We Work Remotely job RSS. Same shape as hn_hiring: company name in
             the title, a real domain usually linked in the job body.

All four are public, unauthenticated, and free. Two other candidates were
tried and rejected after a live check, not on paper: GitHub topic search
(`topic:sre`, `topic:observability`, ...) mostly surfaces companies that BUILD
incident/observability tooling — i.e. Malveon's competitors, not its
customers — plus a lot of hobby repos with no organization behind them.
Indie Hackers has no public API and its pages are client-rendered (empty
HTML, needs a headless browser to read), which this project has already
ruled out on cost grounds. Neither is implemented here.
"""
from __future__ import annotations

import html
import logging
import re

from .. import db
from ..config import cfg
from .base import Record, Sig, find_terms, http
from .social import _key

log = logging.getLogger("discovery")

HREF = re.compile(r'href="(https?://[^"]+)"', re.I)
TAGS = re.compile(r"<[^>]+>")
# Aggregators, ATS links and social — never the company's own domain.
NOT_A_COMPANY = re.compile(
    r"(news\.ycombinator|ycombinator\.com|github\.com|linkedin\.com|twitter\.com|x\.com|youtube|"
    r"weworkremotely\.com|remoteok\.com|remotive\.com|producthunt\.com|"
    r"play\.google\.com|apps\.apple\.com|chrome\.google\.com|"
    r"greenhouse\.io|grnh\.se|lever\.co|ashbyhq|workable|smartrecruiters|angel\.co|wellfound|"
    r"notion\.so|docs\.google|forms\.gle|bit\.ly|tinyurl|medium\.com|substack|gitlab\.com|"
    r"stackoverflow|indeed\.com|glassdoor|mailto|"
    # Recruiting/ATS infrastructure that shows up in these threads constantly.
    r"dover\.com|uctalent|rippling\.com|breezy\.hr|recruitee|jobvite|teamtailor|"
    r"applytojob|bamboohr|hire\.withgoogle|jobs\.|careers\.|apply\.|boards\.|"
    r"airtable\.com|typeform\.com|calendly|discord\.gg|t\.me|wa\.me|"
    # Document and CV hosts — these are where job seekers put their résumé,
    # and they were becoming accounts named "Location: Dakshina Kannada".
    r"drive\.google|docs\.google|acrobat\.adobe|dropbox\.com|icloud\.com|"
    r"read\.cv|standardresume|resume\.io|linktr\.ee|about\.me|carrd\.co|"
    # Demo hosts and default subdomains of free platforms — a Show HN link to
    # one of these names the platform, not the poster's company.
    r"loom\.com|vimeo\.com|notion\.site|"
    r"\.github\.io|\.vercel\.app|\.netlify\.app|\.pages\.dev|\.onrender\.com|"
    r"\.herokuapp\.com|\.repl\.co|\.glitch\.me|\.surge\.sh|\.ngrok(-free)?\.(io|app)|"
    r"\.fly\.dev|\.railway\.app|\.workers\.dev|\.web\.app|\.firebaseapp\.com)", re.I)

# Plenty of posts write "Dave.com | Senior Engineer" or "tpfg.com" with no link
# at all, so a bare-domain fallback roughly doubles the yield. Tight TLD
# allowlist + a stopword list, because "React.js" and "ASP.NET" are not companies.
BARE = re.compile(
    r"\b([a-z0-9][a-z0-9-]{1,40}\.(?:com|io|dev|ai|app|co|net|org|tech|cloud|sh|xyz|so))\b", re.I)
NOT_A_TLD_WORD = {
    "react.js", "vue.js", "node.js", "next.js", "nuxt.js", "three.js", "d3.js",
    "express.js", "ember.js", "backbone.js", "asp.net", "vb.net", "dot.net",
    "socket.io", "chart.js", "jquery.js", "moment.js", "redux.js", "knockout.js",
}


# "Who wants to be hired?" posts and availability blurbs use a fixed header
# shape. Ingesting them creates accounts like drive.google.com named
# "Location: Dakshina Kannada, Karnataka, India Remote: Yes" — a person looking
# for a job, filed as a prospect.
SEEKER = re.compile(
    r"^\s*(location|remote|willing to relocate|r[ée]sum[ée]|resume|seeking|"
    r"technologies|availability|desired role)\s*:", re.I | re.M)
SEEKER_PHRASES = ("willing to relocate", "seeking a role", "looking for my next",
                  "open to work", "available for hire", "my resume", "my résumé")


def _is_seeker(text: str) -> bool:
    plain = TAGS.sub(" ", _unescape(text))[:600]
    low = plain.lower()
    if SEEKER.search(plain):
        return True
    return any(p in low for p in SEEKER_PHRASES)


def _role_from(text: str) -> str | None:
    """Who-is-hiring posts are pipe-delimited: Company | Role | Location | Type.
    The role is the single most useful fact in the post and was being thrown
    away in favour of "posted in Ask HN: Who is hiring?"."""
    head = TAGS.sub(" ", _unescape(text)).split("\n")[0][:400]
    terms = cfg()["roles"]["eng"] + cfg()["roles"]["em"] + cfg()["roles"]["sre"]
    for seg in head.split("|")[1:6]:
        seg = " ".join(seg.split())
        if 3 < len(seg) < 70 and any(t in seg.lower() for t in terms):
            return seg
    return None


def _unescape(t: str) -> str:
    """HN escapes slashes inside href attributes (https:&#x2F;&#x2F;example.com),
    so any URL regex must run AFTER unescaping or it silently matches nothing."""
    return html.unescape(t or "")


def _clean(t: str) -> str:
    return TAGS.sub(" ", _unescape(t))


def _domain_from(text: str) -> str | None:
    raw = _unescape(text)
    for url in HREF.findall(raw):
        if NOT_A_COMPANY.search(url):
            continue
        d = db.norm_domain(url)
        if d:
            return d
    # Fallback: a bare domain in the post body, usually in the header line.
    for m in BARE.findall(TAGS.sub(" ", raw)[:400]):
        if m.lower() in NOT_A_TLD_WORD or NOT_A_COMPANY.search(m):
            continue
        d = db.norm_domain(m)
        if d:
            return d
    return None


class HnHiringCollector:
    """One 'Who is hiring' thread is worth more than a hand-written seed list."""
    name = "hn_hiring"

    def fetch(self):
        spec = cfg()["discovery"]["hn_hiring"]
        if not spec.get("enabled"):
            return
        # search_by_date, not search — relevance ranking returns the 2016 and
        # 2017 threads, and a company that was hiring nine years ago is noise.
        try:
            s = http.json("https://hn.algolia.com/api/v1/search_by_date",
                          params={"query": "Ask HN: Who is hiring", "tags": "story",
                                  "hitsPerPage": spec["threads"] * 8})
        except Exception as exc:
            log.warning("hn_hiring search: %s", exc)
            return

        threads = [h for h in s.get("hits", [])
                   if "who is hiring" in (h.get("title") or "").lower()
                   and (h.get("num_comments") or 0) > 50][:spec["threads"]]
        log.info("hn_hiring: %d threads", len(threads))

        for t in threads:
            try:
                item = http.json(f"https://hn.algolia.com/api/v1/items/{t['objectID']}")
            except Exception as exc:
                log.warning("hn_hiring thread %s: %s", t.get("objectID"), exc)
                continue
            for c in (item.get("children") or [])[:spec["max_per_thread"]]:
                raw = c.get("text") or ""
                if not raw or _is_seeker(raw):
                    continue
                dom = _domain_from(raw)
                if not dom:
                    continue
                text = _clean(raw)
                # "Company | Role | Location | Full Time | ..." is the convention.
                name = " ".join(text.split("|")[0].split())[:60] or None
                if name and ("http" in name or len(name) < 2):
                    name = None
                yield Record(
                    key=f"hnhiring:{c.get('id')}", domain=dom, name=name,
                    body={"text": text[:6000], "thread": t.get("title"),
                          "role": _role_from(raw),
                          "url": f"https://news.ycombinator.com/item?id={c.get('id')}"},
                )

    def signals(self, rec: Record, prev: dict | None):
        c = cfg()
        blob = rec.body["text"].lower()
        dom = rec.domain

        # Publicly hiring engineers. This is deliberately NOT eng_req_surge:
        # a surge means the req count jumped, whereas posting in a monthly
        # thread is simply "we are hiring", which nearly every poster is. Firing
        # the heavier kind here pushed every job ad to intent=100 and buried the
        # accounts with real triggers.
        if any(r in blob for r in c["roles"]["eng"]):
            role = rec.body.get("role")
            month = (rec.body.get("thread") or "").replace("Ask HN: Who is hiring?", "").strip("() ")
            # Name the role. "posted in Ask HN: Who is hiring? (May 2026)" told
            # you nothing you could open an email with.
            detail = (f"hiring {role}" + (f" ({month} HN)" if month else "")) if role \
                else f"posted in {rec.body.get('thread', 'Who is hiring')}"
            yield Sig("hiring_publicly", f"{dom}:hnhiring:{rec.key}",
                      detail=detail, url=rec.body["url"])

        # The post text lists their stack — free technographics.
        for tool, fp in c["fingerprints"].items():
            for term in fp.get("jd", []):
                if term in blob:
                    yield Sig("integration_detected", f"{dom}:integration:{tool}",
                              detail=f"{tool} named in Who-is-hiring post",
                              payload={"tool": tool, "line": fp.get("line")})
                    break

        # Quote the terms they actually used, not the category they fall into.
        hits = find_terms(c["owns_production_terms"], blob)
        if hits:
            yield Sig("owns_production_language", f"{dom}:owns_prod",
                      detail="their post says: " + ", ".join(f"“{h}”" for h in hits[:4]))


class JobFeedCollector:
    """RemoteOK + Remotive.

    Honest limitation: both return a company NAME and no domain, so this cannot
    discover new accounts the way HN and YC do — a name alone is not an entity.
    It enriches accounts you already have, matching by name, with extra hiring
    and stack evidence. Yield is modest and skews non-ICP; it is here for
    completeness, not because it carries the weight of the other two.
    """
    name = "jobfeeds"

    def fetch(self):
        by_name: dict[str, str] = {}
        for r in db.conn().execute(
                "SELECT domain,name FROM account WHERE disqualified IS NULL"):
            for cand in (r["name"], r["domain"].split(".")[0]):
                if cand and len(cand) >= 4:
                    by_name[cand.strip().lower()] = r["domain"]

        rows: list[tuple[str, str, str, str]] = []   # (company, title, text, url)
        try:
            for j in http.json("https://remoteok.com/api") or []:
                if j.get("position"):
                    rows.append((j.get("company") or "", j.get("position") or "",
                                 f"{j.get('description','')} {' '.join(j.get('tags') or [])}",
                                 j.get("url") or ""))
        except Exception as exc:
            log.info("remoteok: %s", exc)
        try:
            d = http.json("https://remotive.com/api/remote-jobs",
                          params={"category": "software-dev", "limit": 200})
            for j in d.get("jobs", []) or []:
                rows.append((j.get("company_name") or "", j.get("title") or "",
                             f"{j.get('description','')} {' '.join(j.get('tags') or [])}",
                             j.get("url") or ""))
        except Exception as exc:
            log.info("remotive: %s", exc)

        matched = 0
        for company, title, text, url in rows:
            dom = by_name.get(company.strip().lower())
            if not dom:
                continue
            matched += 1
            yield Record(key=f"jobfeed:{dom}:{_key(title + company)}", domain=dom,
                         name=company, body={"title": title, "text": text[:5000], "url": url})
        log.info("jobfeeds: %d postings, %d matched a known account", len(rows), matched)

    def signals(self, rec: Record, prev: dict | None):
        c = cfg()
        blob = f"{rec.body['title']} {rec.body['text']}".lower()
        dom = rec.domain
        for tool, fp in c["fingerprints"].items():
            if any(t in blob for t in fp.get("jd", [])):
                yield Sig("integration_detected", f"{dom}:integration:{tool}",
                          detail=f"{tool} named in a remote job posting",
                          payload={"tool": tool, "line": fp.get("line")})
        if find_terms(c["owns_production_terms"], blob):
            yield Sig("owns_production_language", f"{dom}:owns_prod",
                      detail="Their job posting mentions on-call and incident work")
        for pid, p in c["personas"].items():
            found = find_terms(p["pain_terms"], blob)
            hit = found[0] if found else None
            if hit:
                yield Sig("persona_pain_match", f"{dom}:persona:{pid}",
                          detail=f"Their job ad describes the {p['title']} problem: \"{hit}\"",
                          payload={"persona": pid, "term": hit, "line": p["line"]})


class VendorCustomerCollector:
    """Customer showcase pages of the tools Malveon integrates with.

    Every company on Vercel's or Sentry's customers page is CONFIRMED to run
    that integration — no fingerprint inference, no guessing. Yield varies a lot
    because several of these pages render their logo walls in JavaScript, so
    only the links present in the initial HTML are visible here.
    """
    name = "vendors"

    def fetch(self):
        for tool, url in (cfg()["community"]["vendor_customer_pages"] or {}).items():
            try:
                r = http.get(url)
                if r.status_code >= 400:
                    continue
            except Exception as exc:
                log.info("vendors %s: %s", tool, exc)
                continue
            vendor_dom = db.norm_domain(url)
            seen = set()
            for link in HREF.findall(_unescape(r.text)):
                d = db.norm_domain(link)
                if not d or d == vendor_dom or d in seen or NOT_A_COMPANY.search(link):
                    continue
                seen.add(d)
                yield Record(key=f"vendor:{tool}:{d}", domain=d,
                             body={"tool": tool, "page": url})
            log.info("vendors: %s -> %d candidate companies", tool, len(seen))

    def signals(self, rec: Record, prev: dict | None):
        tool = rec.body["tool"]
        fp = cfg()["fingerprints"].get(tool) or {}
        yield Sig("integration_detected", f"{rec.domain}:integration:{tool}",
                  detail=f"{tool} (listed on their customers page)",
                  url=rec.body["page"], payload={"tool": tool, "line": fp.get("line")})


class YcCollector:
    """The YC directory is an ICP-shaped list of thousands of companies."""
    name = "yc"

    def fetch(self):
        spec = cfg()["discovery"]["yc"]
        if not spec.get("enabled"):
            return
        # The directory is ~250 pages — far more than one run should fetch. A
        # fixed range(1, pages) rescans the SAME first N pages every run, so
        # the count plateaus the moment those are exhausted. Instead, resume
        # from wherever the last run stopped and wrap back to page 1 once the
        # end is reached, so every run makes forward progress and old ground
        # eventually gets rechecked for newly added companies too.
        page = (db.get_snapshot("yc", "_cursor") or {}).get("next_page", 1)
        start = page
        seen = 0
        for _ in range(spec["pages"]):
            try:
                d = http.json("https://api.ycombinator.com/v0.1/companies",
                              params={"page": page})
            except Exception as exc:
                log.warning("yc page %d: %s", page, exc)
                break
            companies = d.get("companies") or []
            total_pages = d.get("totalPages") or page
            for co in companies:
                dom = db.norm_domain(co.get("website") or "")
                if not dom:
                    continue
                size = co.get("team_size")
                # Unknown size is kept — the ICP band is applied later, once
                # enrichment supplies a real number. Only exclude known misfits.
                if isinstance(size, int) and not (spec["min_team_size"] <= size <= spec["max_team_size"]):
                    continue
                seen += 1
                yield Record(
                    key=f"yc:{co.get('id') or dom}", domain=dom, name=co.get("name"),
                    body={"batch": co.get("batch"), "team_size": size,
                          "one_liner": co.get("one_liner") or "",
                          "industry": co.get("industry") or "",
                          "tags": co.get("tags") or []},
                    account_fields={"headcount": size if isinstance(size, int) else None},
                )
            page = page + 1 if page < total_pages else 1
        db.put_snapshot("yc", "_cursor", {"next_page": page})
        log.info("yc: %d companies in band (scanned pages %d..%d)", seen, start, page)

    def signals(self, rec: Record, prev: dict | None):
        b = rec.body
        blob = f"{b.get('one_liner','')} {b.get('industry','')} {' '.join(b.get('tags') or [])}".lower()
        # Non-software YC companies (biotech, hardware, fintech-only) are not ICP.
        if not any(k in blob for k in ("software", "saas", "developer", "infrastructure",
                                       "devtool", "api", "platform", "b2b", "analytics",
                                       "security", "data", "ai", "cloud")):
            return
        # `discovered` is deliberately absent from the intent config, so it
        # scores zero. Discovery is provenance, not intent — a company existing
        # is not a company wanting to buy.
        yield Sig("discovered", f"{rec.domain}:yc:{b.get('batch')}",
                  detail=f"Y Combinator {b.get('batch')} company · {b.get('one_liner','')[:90]}",
                  url=f"https://www.ycombinator.com/companies?query={rec.domain}")


class ShowHnCollector:
    """"Show HN: I built X" — people launching their own product. The `url`
    field on these posts already points at the product/company site, no href
    scraping needed. Noisier than hn_hiring by nature (plenty of weekend
    projects with no company behind them), so this is discovery-only, same as
    YcCollector — existing is not wanting to buy, and a company with nothing
    else about it just sits at zero intent until something real happens.
    """
    name = "show_hn"

    def fetch(self):
        spec = cfg()["discovery"]["show_hn"]
        if not spec.get("enabled"):
            return
        try:
            s = http.json("https://hn.algolia.com/api/v1/search_by_date",
                          params={"tags": "show_hn", "hitsPerPage": spec["max_posts"]})
        except Exception as exc:
            log.warning("show_hn search: %s", exc)
            return
        kept = 0
        for h in s.get("hits", []):
            url = h.get("url") or ""
            if not url or NOT_A_COMPANY.search(url):
                continue
            dom = db.norm_domain(url)
            if not dom:
                continue
            title = (h.get("title") or "").split("Show HN:", 1)[-1].strip()
            kept += 1
            yield Record(
                key=f"showhn:{h.get('objectID')}", domain=dom,
                body={"title": title[:200],
                      "url": f"https://news.ycombinator.com/item?id={h.get('objectID')}"},
            )
        log.info("show_hn: %d posts kept of %d", kept, len(s.get("hits", [])))

    def signals(self, rec: Record, prev: dict | None):
        yield Sig("discovered", f"{rec.domain}:showhn",
                  detail=f"Showed their product on HN: {rec.body['title'][:90]}",
                  url=rec.body["url"])


class WeWorkRemotelyCollector:
    """We Work Remotely's category RSS feeds. Same shape as hn_hiring — the
    title is "Company: Role" and the job body is full HTML, usually with a
    link back to the company's own site — so this reuses the same href/bare-
    domain extraction rather than a second parser.
    """
    name = "wwr"

    CATEGORIES = ("remote-programming-jobs", "remote-devops-sysadmin-jobs")
    ITEM = re.compile(r"<item>(.*?)</item>", re.S)
    FIELD = re.compile(r"<title>(.*?)</title>.*?<description>(.*?)</description>",
                       re.S)

    def fetch(self):
        spec = cfg()["discovery"]["wwr"]
        if not spec.get("enabled"):
            return
        seen_ids = 0
        for cat in self.CATEGORIES:
            try:
                r = http.get(f"https://weworkremotely.com/categories/{cat}.rss")
                if r.status_code >= 400:
                    continue
            except Exception as exc:
                log.info("wwr %s: %s", cat, exc)
                continue
            for item in self.ITEM.findall(r.text)[:spec["max_per_category"]]:
                m = self.FIELD.search(item)
                if not m:
                    continue
                title, body = m.group(1), m.group(2)
                if ":" not in title:
                    continue
                company, role = (p.strip() for p in title.split(":", 1))
                dom = _domain_from(body)
                if not dom:
                    continue
                seen_ids += 1
                yield Record(
                    key=f"wwr:{_key(cat + title)}", domain=dom, name=company or None,
                    body={"role": role[:120], "text": _clean(body)[:6000],
                          "url": f"https://weworkremotely.com/categories/{cat}"},
                )
        log.info("wwr: %d postings matched a domain", seen_ids)

    def signals(self, rec: Record, prev: dict | None):
        c = cfg()
        blob = rec.body["text"].lower()
        dom = rec.domain

        yield Sig("hiring_publicly", f"{dom}:wwr:{rec.key}",
                  detail=f"hiring {rec.body['role']} (We Work Remotely)", url=rec.body["url"])

        for tool, fp in c["fingerprints"].items():
            for term in fp.get("jd", []):
                if term in blob:
                    yield Sig("integration_detected", f"{dom}:integration:{tool}",
                              detail=f"{tool} named in a We Work Remotely job post",
                              payload={"tool": tool, "line": fp.get("line")})
                    break

        hits = find_terms(c["owns_production_terms"], blob)
        if hits:
            yield Sig("owns_production_language", f"{dom}:owns_prod",
                      detail="their post says: " + ", ".join(f"“{h}”" for h in hits[:4]))
