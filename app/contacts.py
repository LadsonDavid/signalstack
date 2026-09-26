"""Contact discovery + email verification waterfall.

Four free layers run before a single paid credit is spent, and every result is
cached permanently:

  1. RFC-5322 syntax
  2. MX record lookup            (DNS/53 — not blocked on Railway)
  3. role / disposable / freemail rejection
  4. pattern inference           (learn the domain's format from a known-good
                                  address before guessing)
  5. MillionVerifier over HTTPS  <- the only paid call

Why not the SMTP RCPT TO handshake from the research doc: Railway blocks
outbound 25/465/587/2525 on Hobby (Pro is $20/mo, 40% of the budget), and even
unblocked, Gmail and Microsoft 365 — most of B2B — answer "unknown" for valid
addresses, which both source documents state. That one step is bought for about
$0.004; everything around it is built here.

Because Malveon is a $99/mo flat, credit-card purchase, the target is ONE
decision maker per account, not a buying committee. That is what keeps the
verification bill near zero.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from datetime import datetime, timedelta, timezone

import dns.resolver
import httpx

from . import db
from .config import Env, cfg
from .collectors.base import http

log = logging.getLogger("contacts")

SYNTAX = re.compile(r"^[A-Za-z0-9!#$%&'*+/=?^_`{|}~.-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
ROLE = {"info", "sales", "support", "hello", "contact", "admin", "help", "team",
        "billing", "careers", "jobs", "press", "legal", "security", "noreply",
        "no-reply", "marketing", "office", "enquiries", "hi"}
FREEMAIL = {"gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "icloud.com",
            "aol.com", "proton.me", "protonmail.com", "gmx.com", "mail.com"}
DISPOSABLE = {"mailinator.com", "10minutemail.com", "guerrillamail.com", "tempmail.com",
              "yopmail.com", "trashmail.com", "sharklasers.com", "getnada.com"}
REVERIFY_DAYS = 90


def _ascii(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    return re.sub(r"[^a-z]", "", s.encode("ascii", "ignore").decode().lower())


def mx_ok(domain: str) -> bool:
    try:
        return bool(dns.resolver.resolve(domain, "MX", lifetime=6))
    except Exception:
        return False


def _fmt(pat: str, first: str, last: str) -> str:
    f, l = _ascii(first), _ascii(last)
    return pat.format(f=f, l=l, fi=f[:1], li=l[:1])


def derive_pattern(first: str, last: str, local: str) -> str | None:
    """Recover the house format from one known-good address.

    This is what makes guessing reliable rather than a coin flip: given
    "Liz Fong-Jones" and a real lizf@honeycomb.io scraped from public commit
    history, we learn the domain uses {f}{li} and can then hit the CTO first try.
    """
    if not first or not local:
        return None
    for pat in cfg()["email_patterns"]:
        try:
            if _fmt(pat, first, last) == local.lower():
                return pat
        except Exception:
            continue
    return None


def house_pattern(domain: str) -> str | None:
    """Most common derived pattern for a domain, from contacts already known."""
    rows = db.conn().execute(
        "SELECT name,email FROM contact WHERE email LIKE ? AND name IS NOT NULL "
        "AND status IN ('valid','pattern_confirmed')", (f"%@{domain}",)).fetchall()
    votes: dict[str, int] = {}
    for r in rows:
        parts = (r["name"] or "").split()
        if len(parts) < 2:
            continue
        pat = derive_pattern(parts[0], parts[-1], r["email"].split("@")[0])
        if pat:
            votes[pat] = votes.get(pat, 0) + 1
    return max(votes, key=votes.get) if votes else None


def guess_emails(first: str, last: str, domain: str) -> list[str]:
    f, l = _ascii(first), _ascii(last)
    if not f or not domain:
        return []
    patterns = list(cfg()["email_patterns"])
    learned = house_pattern(domain)
    if learned:
        patterns = [learned] + [p for p in patterns if p != learned]
    out, seen = [], set()
    for p in patterns:
        if not l and "{l}" in p:
            continue
        e = _fmt(p, first, last) + "@" + domain
        if e not in seen:
            seen.add(e)
            out.append(e)
    return out


def verify(email: str, allow_paid: bool = True) -> dict:
    """Run the waterfall. Returns {status, reason, provider}."""
    email = (email or "").strip().lower()
    if not SYNTAX.match(email):
        return {"status": "invalid", "reason": "syntax", "provider": "local"}

    local, domain = email.rsplit("@", 1)
    if local in ROLE:
        return {"status": "invalid", "reason": "role address", "provider": "local"}
    if domain in FREEMAIL:
        return {"status": "invalid", "reason": "freemail", "provider": "local"}
    if domain in DISPOSABLE:
        return {"status": "invalid", "reason": "disposable", "provider": "local"}

    cached = db.conn().execute(
        "SELECT status,checked_at,provider FROM email_verification WHERE email=?",
        (email,)).fetchone()
    if cached:
        age = datetime.now(timezone.utc) - datetime.fromisoformat(cached["checked_at"])
        if age < timedelta(days=REVERIFY_DAYS):
            return {"status": cached["status"], "reason": "cached", "provider": cached["provider"]}

    if not mx_ok(domain):
        _cache(email, "invalid", "local", "no MX")
        return {"status": "invalid", "reason": "no MX record", "provider": "local"}

    if not (allow_paid and Env.MILLIONVERIFIER_KEY):
        return {"status": "unknown", "reason": "passed free checks; not verified",
                "provider": "local"}

    try:
        d = http.json("https://api.millionverifier.com/api/v3/", params={
            "api": Env.MILLIONVERIFIER_KEY, "email": email, "timeout": 10})
    except Exception as exc:
        log.warning("millionverifier failed for %s: %s", email, exc)
        return {"status": "unknown", "reason": f"verifier error: {exc}", "provider": "local"}

    raw = (d.get("result") or "").lower()
    status = {"ok": "valid", "catch_all": "catch_all", "unknown": "unknown",
              "disposable": "invalid", "invalid": "invalid", "error": "unknown"}.get(raw, "unknown")
    _cache(email, status, "millionverifier", str(d))
    return {"status": status, "reason": d.get("subresult") or raw, "provider": "millionverifier"}


def _cache(email: str, status: str, provider: str, raw: str) -> None:
    with db.tx() as c:
        c.execute("INSERT OR REPLACE INTO email_verification"
                  "(email,status,provider,checked_at,raw) VALUES(?,?,?,?,?)",
                  (email, status, provider, db.now(), raw[:2000]))


# ---------------------------------------------------------------- discovery

def pdl_company(domain: str) -> dict | None:
    """Free tier: 100 lookups/month. Only ever called for accounts that already
    pass the fit gates, which is what keeps it inside the free tier."""
    if not Env.PDL_KEY:
        return None
    try:
        return http.json("https://api.peopledatalabs.com/v5/company/enrich",
                         params={"website": domain},
                         headers={"X-Api-Key": Env.PDL_KEY})
    except Exception as exc:
        log.info("pdl company %s: %s", domain, exc)
        return None


_pdl_people_available = True


def pdl_people(domain: str, limit: int) -> list[dict]:
    """Person search is NOT included in PDL's free tier — it answers 400.

    One failure disables it for the process rather than retrying once per
    account, which turned a 20-account enrichment run into 20 stack traces.
    Contacts come from GitHub commit history instead, which is free and better.
    """
    global _pdl_people_available
    if not Env.PDL_KEY or not _pdl_people_available:
        return []
    titles = " OR ".join(f'job_title:"{t}"' for t in cfg()["target_titles"])
    try:
        d = http.json("https://api.peopledatalabs.com/v5/person/search",
                      params={"query": f'job_company_website:"{domain}" AND ({titles})',
                              "size": limit},
                      headers={"X-Api-Key": Env.PDL_KEY})
        return d.get("data", []) or []
    except Exception as exc:
        if "400" in str(exc) or "403" in str(exc):
            _pdl_people_available = False
            log.info("pdl person search unavailable on this plan — using GitHub for contacts")
        else:
            log.info("pdl people %s: %s", domain, exc)
        return []


# ------------------------------------------------- free discovery via GitHub

# Service accounts commit constantly and look exactly like people. A CI robot's
# address on your board is a wasted send and an instant credibility loss.
NOREPLY = re.compile(
    r"noreply|no-reply|\[bot\]|users\.noreply\.github\.com|"
    r"\+.*bot|githubbot|github-actions|dependabot|renovate|"
    # Any separator before "bot": langfuse-bot@, deploy.bot@, ci_bot@.
    r"(^|[-._+])(bot|robot|automation|noreply)@|bot@|"
    r"^(ci|cd|build|deploy|jenkins|actions|automation|robot|bot|svc|service)[@+.-]", re.I)
SENIOR = ["cto", "chief technology", "vp of eng", "vp eng", "head of eng", "founder",
          "co-founder", "director of eng", "engineering manager", "head of platform",
          "principal engineer", "staff engineer", "sre", "devops", "platform lead"]


def _gh(path: str, **params):
    hdr = {"Accept": "application/vnd.github+json"}
    if Env.GITHUB_TOKEN:
        hdr["Authorization"] = f"Bearer {Env.GITHUB_TOKEN}"
    return http.json(f"https://api.github.com{path}", params=params or None, headers=hdr)


def github_org(domain: str) -> str | None:
    """Resolve a domain to its GitHub org, verified via the org's own blog URL.

    A blanket `except: continue` here made a rate limit, a rejected token and a
    genuine 404 completely indistinguishable — the collector just reported
    "no resolvable GitHub org" for every account and looked like it was working.
    Auth and rate-limit failures now abort loudly instead of being swallowed as
    misses.
    """
    label = domain.split(".")[0]
    for cand in (label, label + "io", label + "hq", label + "-inc", domain.replace(".", "")):
        try:
            o = _gh(f"/orgs/{cand}")
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            if code == 404:
                continue                      # genuine miss, try the next guess
            if code in (401, 403, 429):
                remaining = exc.response.headers.get("x-ratelimit-remaining")
                raise RuntimeError(
                    f"GitHub refused the request ({code}"
                    + (f", rate-limit remaining={remaining}" if remaining else "")
                    + "). This is an auth/quota problem, not a missing org."
                ) from exc
            continue
        except Exception as exc:
            log.info("github_org %s/%s: %s", domain, cand, exc)
            continue
        blog = (o.get("blog") or "").lower()
        if domain in blog or domain in (o.get("email") or "").lower():
            return o.get("login")
    return None


def github_contacts(domain: str, max_people: int = 25) -> dict:
    """Names + real corporate emails from public GitHub data. Costs nothing.

    Malveon's ICP is engineering orgs, so its decision makers are almost always
    on GitHub under the company org. Two independent harvests:
      * public org members -> real names, bios, occasionally a public email
      * public commit authors -> real @domain addresses, self-published in git
    The commit emails are what teach us the house pattern, which is then used to
    construct an address for the senior people who never expose one.
    """
    org = github_org(domain)
    if not org:
        return {"org": None, "found": 0}

    # --- harvest real addresses from public commit history -----------------
    seen_emails: dict[str, str] = {}       # email -> author name
    try:
        repos = _gh(f"/orgs/{org}/repos", per_page=8, sort="pushed")
    except Exception:
        repos = []
    for repo in (repos if isinstance(repos, list) else [])[:8]:
        try:
            commits = _gh(f"/repos/{org}/{repo['name']}/commits", per_page=100)
        except Exception:
            continue
        for cm in commits if isinstance(commits, list) else []:
            a = (cm.get("commit") or {}).get("author") or {}
            email, name = (a.get("email") or "").lower(), a.get("name") or ""
            if email.endswith(f"@{domain}") and not NOREPLY.search(email):
                seen_emails.setdefault(email, name)

    aid = db.upsert_account(domain)
    stored = 0
    for email, name in seen_emails.items():
        with db.tx() as c:
            cur = c.execute(
                "INSERT OR IGNORE INTO contact(account_id,name,title,email,status,source,created_at)"
                " VALUES(?,?,?,?,?,?,?)",
                (aid, name, "", email, "pattern_confirmed", "github:commits", db.now()))
            stored += cur.rowcount

    pattern = house_pattern(domain)

    # --- senior people from the public member list -------------------------
    try:
        members = _gh(f"/orgs/{org}/members", per_page=max_people)
    except Exception:
        members = []
    people = []
    for m in (members if isinstance(members, list) else [])[:max_people]:
        try:
            u = _gh(f"/users/{m['login']}")
        except Exception:
            continue
        blob = f"{u.get('bio') or ''} {u.get('company') or ''}".lower()
        rank = next((i for i, t in enumerate(SENIOR) if t in blob), 99)
        people.append({"login": u.get("login"), "name": u.get("name") or "",
                       "bio": (u.get("bio") or "")[:120],
                       "email": (u.get("email") or "").lower(), "rank": rank})
    people.sort(key=lambda p: p["rank"])

    # Never guess an address for someone whose real one we already harvested —
    # that is how Liz Fong-Jones ends up in the list twice, once as the genuine
    # lizf@ and once as a fabricated lizfongjones@ that hard-bounces.
    known_names = {n.strip().lower() for n in seen_emails.values() if n}

    for p in people:
        if p["rank"] == 99 and not p["email"]:
            continue                                   # not a decision maker, no address
        if p["name"].strip().lower() in known_names:
            continue
        parts = p["name"].split()
        email = p["email"] if p["email"].endswith(f"@{domain}") else None
        # A self-published address is real; anything we construct is a guess and
        # must say so, because the status field is what tells you whether it is
        # safe to send without spending a verification credit.
        status = "pattern_confirmed"
        if not email and len(parts) >= 2:
            cands = guess_emails(parts[0], parts[-1], domain)
            email = cands[0] if cands else None
            status = "guessed" if pattern else "guessed_no_pattern"
        if not email:
            continue
        with db.tx() as c:
            cur = c.execute(
                "INSERT OR IGNORE INTO contact(account_id,name,title,email,status,source,created_at)"
                " VALUES(?,?,?,?,?,?,?)",
                (aid, p["name"], p["bio"], email, status, "github:members", db.now()))
            stored += cur.rowcount

    return {"org": org, "pattern": pattern, "commit_emails": len(seen_emails),
            "members": len(people), "stored": stored}


def enrich_account(domain: str, verify_paid: bool = True) -> dict:
    """Headcount + up to N decision makers with a verified email."""
    aid = db.upsert_account(domain)
    if not aid:
        return {"error": "bad domain"}

    # Free source first — for engineering orgs it often beats the paid one, and
    # it teaches us the house email pattern either way.
    gh = github_contacts(domain)

    co = pdl_company(domain)
    if co and co.get("employee_count"):
        with db.tx() as c:
            c.execute("UPDATE account SET headcount=? WHERE id=?", (co["employee_count"], aid))
        f = cfg()["fit"]["disqualify"]
        n = co["employee_count"]
        if n > f["headcount_over"]:
            db.disqualify(aid, f"{n} employees — above ICP band")
        elif n < f["headcount_under"]:
            db.disqualify(aid, f"{n} employees — below ICP band")

    added = []
    for p in pdl_people(domain, cfg()["contacts_per_account"]):
        first = p.get("first_name") or ""
        last = p.get("last_name") or ""
        title = p.get("job_title") or ""
        email = next((e for e in (p.get("work_email"), *(p.get("emails") or []))
                      if isinstance(e, str) and e.endswith(f"@{domain}")), None)
        cands = [email] if email else guess_emails(first, last, domain)
        chosen, status = None, "unverified"
        for cand in cands[:4]:
            res = verify(cand, allow_paid=verify_paid)
            if res["status"] in ("valid", "catch_all"):
                chosen, status = cand, res["status"]
                break
            if res["status"] == "unknown" and not chosen:
                chosen, status = cand, "unknown"
        if not chosen:
            continue
        with db.tx() as c:
            c.execute("INSERT OR IGNORE INTO contact"
                      "(account_id,name,title,email,status,source,created_at) "
                      "VALUES(?,?,?,?,?,?,?)",
                      (aid, f"{first} {last}".strip(), title, chosen, status, "pdl", db.now()))
        added.append({"name": f"{first} {last}".strip(), "title": title,
                      "email": chosen, "status": status})

    return {"domain": domain, "headcount": (co or {}).get("employee_count"),
            "github": gh, "pdl_contacts": added,
            "total": db.conn().execute(
                "SELECT COUNT(*) FROM contact WHERE account_id=?", (aid,)).fetchone()[0]}
