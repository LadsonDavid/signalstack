"""malveon.com visitor identification — the highest-value signal in the system.

Everything else in this codebase infers intent from the outside. This observes
it directly: someone reading your pricing page is in a buying motion, not being
guessed at.

Resolution is free-first:
  1. reverse DNS (PTR) — no API key, no cost. Corporate networks frequently
     publish a PTR containing the company's own domain.
  2. ipinfo.io — optional, free tier 50k/month. Note its free tier returns the
     ISP/ASN name (`org`), NOT a company domain, so the org string is matched
     against accounts already in the universe rather than trusted as a domain.
  3. unresolved — still recorded. An unidentified visit is real traffic, and a
     better resolver later can re-run over the stored rows.

Bot rejection is not optional here. Automated traffic now outweighs human
traffic on the open web, and a naive reverse-IP pipeline fills up with crawlers
scraping pricing pages from datacenters — which would fire your best signal on
a robot. Three gates: user-agent, dwell time, and an ISP/hosting denylist.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

import dns.resolver
import dns.reversename

from . import db
from .config import Env, cfg
from .collectors.base import http

log = logging.getLogger("tracking")

PRIVATE = re.compile(r"^(10\.|127\.|169\.254\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.|::1|fc|fd)", re.I)
IP_CACHE_DAYS = 30


def is_bot(ua: str, seconds: float | None) -> bool:
    t = cfg()["tracking"]
    low = (ua or "").lower()
    if not low:
        return True
    if any(b in low for b in t["bot_ua"]):
        return True
    if seconds is not None and seconds < t["min_seconds_on_page"]:
        return True
    return False


def _is_isp(org: str) -> bool:
    low = (org or "").lower()
    return any(k in low for k in cfg()["tracking"]["ignore_org"])


# Domains that are infrastructure, never a prospect, even when a PTR points at
# them. dns.google, ec2.amazonaws.com and friends resolve cleanly and look like
# real companies to a naive check.
INFRA_DOMAINS = {
    "google", "googleusercontent.com", "dns.google", "amazonaws.com", "aws.com",
    "azure.com", "cloudapp.net", "digitalocean.com", "linode.com", "vultr.com",
    "hetzner.com", "hetzner.de", "ovh.net", "ovh.com", "cloudflare.com",
    "akamaitechnologies.com", "fastly.net", "1e100.net", "comcast.net",
    "verizon.net", "rr.com", "jio.com", "airtel.in", "t-mobile.com",
}


def _is_infra_domain(d: str) -> bool:
    low = (d or "").lower()
    return low in INFRA_DOMAINS or any(low.endswith("." + x) for x in INFRA_DOMAINS)


def _ptr_domain(ip: str) -> str | None:
    """Reverse DNS. Free, no key. Corporate PTRs often carry the real domain."""
    try:
        rev = dns.reversename.from_address(ip)
        for rd in dns.resolver.resolve(rev, "PTR", lifetime=4):
            host = str(rd).rstrip(".").lower()
            parts = host.split(".")
            if len(parts) >= 2:
                cand = db.norm_domain(".".join(parts[-2:]))
                if cand and not _is_isp(cand):
                    return cand
    except Exception:
        pass
    return None


def _ipinfo(ip: str) -> dict:
    if cfg()["tracking"].get("provider") != "ipinfo":
        return {}
    try:
        params = {"token": Env.IPINFO_TOKEN} if Env.IPINFO_TOKEN else None
        return http.json(f"https://ipinfo.io/{ip}/json", params=params) or {}
    except Exception as exc:
        log.info("ipinfo %s: %s", ip, exc)
        return {}


def resolve_company(ip: str) -> tuple[str | None, str, str]:
    """Returns (domain_or_None, org_string, how)."""
    row = db.conn().execute("SELECT * FROM ip_cache WHERE ip=?", (ip,)).fetchone()
    if row:
        age = datetime.now(timezone.utc) - datetime.fromisoformat(row["checked_at"])
        if age < timedelta(days=IP_CACHE_DAYS):
            return row["domain"], row["org"] or "", row["resolution"]

    domain, org, how = None, "", "none"

    d = _ptr_domain(ip)
    if d and not _is_infra_domain(d):
        domain, how = d, "ptr"

    if not domain:
        info = _ipinfo(ip)
        org = (info.get("org") or "").strip()
        host = (info.get("hostname") or "").lower()
        # Check the ORG first and let it veto the hostname. 8.8.8.8 has the
        # hostname dns.google, which passes a domain-only ISP check and would
        # put "dns.google" on the board as a visiting company — while its org
        # string says "AS15169 Google LLC" in plain sight.
        if _is_isp(org):
            how = "isp"
        else:
            if host:
                parts = host.rstrip(".").split(".")
                cand = db.norm_domain(".".join(parts[-2:])) if len(parts) >= 2 else None
                if cand and not _is_isp(cand):
                    domain, how = cand, "ipinfo_hostname"
            if not domain and org:
                # Free-tier ipinfo gives an ISP/ASN name, not a domain — so the
                # only safe use is matching it to a company already known here.
                clean = re.sub(r"^AS\d+\s+", "", org).strip()
                hit = db.conn().execute(
                    "SELECT domain FROM account WHERE name LIKE ? LIMIT 1",
                    (f"%{clean}%",)).fetchone() if len(clean) >= 4 else None
                if hit:
                    domain, how = hit["domain"], "ipinfo_org_match"

    with db.tx() as c:
        c.execute("INSERT OR REPLACE INTO ip_cache(ip,domain,org,resolution,checked_at) "
                  "VALUES(?,?,?,?,?)", (ip, domain, org, how, db.now()))
    return domain, org, how


def record_visit(ip: str, path: str, ua: str, referrer: str, seconds: float | None,
                 via: str | None = None) -> dict:
    """Store a hit and, if it resolves to a company, score it.

    `via` is the company tag from a tracked outreach link
    (malveon.com/?via=acme.com). It beats IP lookup outright and is the whole
    point of email tracking: reverse-IP can only ever name companies large
    enough to own an ASN, so for a 10-150 person prospect it returns the
    visitor's ISP and nothing usable. A tagged link is certain instead of
    probabilistic — you already know who you sent it to.
    """
    if not cfg()["tracking"].get("enabled"):
        return {"ok": False, "reason": "tracking disabled"}
    ip = (ip or "").split(",")[0].strip()
    if not ip or PRIVATE.match(ip):
        return {"ok": False, "reason": "private or missing ip"}

    if is_bot(ua, seconds):
        with db.tx() as c:
            c.execute("INSERT INTO site_visit(ip,path,referrer,user_agent,seconds,resolution,created_at)"
                      " VALUES(?,?,?,?,?,?,?)", (ip, path, referrer, ua, seconds, "bot", db.now()))
        return {"ok": True, "resolution": "bot"}

    tagged = db.norm_domain(via) if via else None
    if tagged:
        domain, org, how = tagged, "", "email_link"
    else:
        domain, org, how = resolve_company(ip)
    aid = db.upsert_account(domain, name=org or None) if domain else None

    with db.tx() as c:
        c.execute("INSERT INTO site_visit"
                  "(ip,path,referrer,user_agent,seconds,org,account_id,resolution,created_at)"
                  " VALUES(?,?,?,?,?,?,?,?,?)",
                  (ip, path, referrer, ua, seconds, org, aid, how, db.now()))

    if not aid:
        return {"ok": True, "resolution": how, "identified": False}

    t = cfg()["tracking"]
    hot = any((path or "").startswith(p) for p in t["pricing_paths"])
    kind = "site_visit_pricing" if hot else "site_visit"
    # Bucket by session so a single reader refreshing does not stack signals.
    bucket = db.now()[:13]
    who = "Clicked your email and " if how == "email_link" else ""
    db.add_signal(aid, kind, "tracking", f"{domain}:{kind}:{bucket}",
                  detail=f"{who}visited {path or '/'}" + (" — looking at pricing" if hot else ""),
                  url=f"https://{cfg()['product']['domain']}{path or '/'}",
                  payload={"ip_org": org, "how": how, "referrer": referrer})
    log.info("site visit identified: %s (%s) -> %s", domain, how, path)
    return {"ok": True, "resolution": how, "identified": True, "domain": domain}


PIXEL_JS = """/* Malveon lead pixel. Add to malveon.com:
   <script async src="https://YOUR-APP-HOST/px.js"></script>  */
(function () {
  var start = Date.now(), sent = false;
  // Company tag from a tracked outreach link: malveon.com/?via=acme.com
  // Stored in sessionStorage because the tag only appears on the FIRST page
  // they land on. Without persisting it, a visitor who clicks through to
  // /pricing (the page you actually care about) arrives with no tag and falls
  // back to an unusable ISP lookup.
  var via = "";
  try {
    var q = new URLSearchParams(location.search);
    via = q.get("via") || q.get("utm_company") || "";
    if (via) sessionStorage.setItem("mv_via", via);
    else via = sessionStorage.getItem("mv_via") || "";
  } catch (e) {}
  function send() {
    if (sent) return; sent = true;
    var s = Math.round((Date.now() - start) / 1000);
    var u = "%(base)s/px?p=" + encodeURIComponent(location.pathname)
          + "&r=" + encodeURIComponent(document.referrer || "")
          + "&s=" + s
          + (via ? "&via=" + encodeURIComponent(via) : "");
    if (navigator.sendBeacon) navigator.sendBeacon(u);
    else { var i = new Image(); i.src = u; }
  }
  // Fire once the visit is long enough to be a human reading, not a crawler.
  setTimeout(send, 4000);
  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState === "hidden") send();
  });
})();
"""
