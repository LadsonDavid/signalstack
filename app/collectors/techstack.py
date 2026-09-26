"""Technographic fingerprinting — the fit engine.

Malveon's 22 integrations ARE the ICP checklist, so this needs ~35 fingerprints,
not Wappalyzer's 7,200. Four concurrent layers, deduplicated into one result:

  1. HTTP response headers   x-vercel-id, cf-ray, x-render-origin-server...
  2. DNS CNAME chain         catches hosts that leave no HTTP trace at all
  3. Page source             Sentry / LaunchDarkly / Datadog RUM script tags
  4. Status-page probe       status.<domain> — hard proof they own production

No headless browser: a plain GET catches hosting headers and the script tags
present in initial HTML, and Chrome on Railway costs real memory. If the
measured miss rate ever justifies it, that is the upgrade path.
"""
from __future__ import annotations

import logging
import urllib.robotparser
from functools import lru_cache

import dns.resolver
import httpx

from .. import db
from ..config import cfg
from .base import Record, Sig, fan_out, http

log = logging.getLogger("techstack")

MAX_BODY = 500_000
STATUS_HOSTS = ("statuspage.io", "status.io", "betteruptime", "betterstack",
                "instatus", "statuspal", "sorryapp", "uptime.com")


@lru_cache(maxsize=2048)
def _robots_ok(domain: str) -> bool:
    """Respect robots.txt on our own crawler. Unreachable robots => allowed."""
    rp = urllib.robotparser.RobotFileParser()
    rp.set_url(f"https://{domain}/robots.txt")
    try:
        r = http.get(f"https://{domain}/robots.txt")
        if r.status_code >= 400:
            return True
        rp.parse(r.text.splitlines())
        return rp.can_fetch(cfg()["http"]["user_agent"], f"https://{domain}/")
    except Exception:
        return True


def _get(url: str) -> httpx.Response | None:
    for verify in (True, False):   # read-only fingerprinting; bad certs are common
        try:
            return http.get(url, verify=verify)
        except Exception:
            continue
    return None


def _cnames(host: str) -> list[str]:
    out = []
    for name in (host, f"www.{host}"):
        try:
            for rd in dns.resolver.resolve(name, "CNAME", lifetime=5):
                out.append(str(rd.target).rstrip(".").lower())
        except Exception:
            continue
    return out


def probe(domain: str) -> dict:
    """Collect raw evidence for one domain. Never raises."""
    ev = {"headers": {}, "server": "", "body": "", "cnames": [], "status_page": None}
    if not _robots_ok(domain):
        log.info("robots.txt disallows %s — skipping", domain)
        return ev

    for host in (domain, f"app.{domain}"):
        r = _get(f"https://{host}")
        if not r:
            continue
        for k, v in r.headers.items():
            ev["headers"][k.lower()] = v
        ev["server"] += " " + r.headers.get("server", "").lower()
        ev["body"] += r.text[:MAX_BODY]

    ev["cnames"] = _cnames(domain)

    # Status page: a subdomain that resolves, or a link in the homepage.
    sr = _get(f"https://status.{domain}")
    if sr is not None and sr.status_code < 400:
        ev["status_page"] = f"https://status.{domain}"
    else:
        low = ev["body"].lower()
        for h in STATUS_HOSTS:
            if h in low:
                ev["status_page"] = h
                break
    return ev


def detect(ev: dict) -> dict[str, str]:
    """Evidence -> {tool: how_we_know}. Deduplicated across all four layers."""
    found: dict[str, str] = {}
    hdr = ev.get("headers", {})
    body = (ev.get("body") or "").lower()
    server = ev.get("server") or ""
    cn = " ".join(ev.get("cnames") or [])

    for tool, fp in cfg()["fingerprints"].items():
        for h in fp.get("headers", []):
            if h.lower() in hdr:
                found[tool] = f"header {h}"
                break
        if tool in found:
            continue
        for s in fp.get("server", []):
            if s in server:
                found[tool] = f"server: {s}"
                break
        if tool in found:
            continue
        for c in fp.get("cname", []):
            if c in cn:
                found[tool] = f"CNAME -> {c}"
                break
        if tool in found:
            continue
        for b in fp.get("body", []):
            if b.lower() in body:
                found[tool] = "in page source"
                break
    return found


class TechstackCollector:
    name = "techstack"

    def __init__(self, only_domain: str | None = None):
        self.only = only_domain

    def fetch(self):
        q = ("SELECT domain,name FROM account WHERE disqualified IS NULL"
             + (" AND domain=?" if self.only else ""))
        rows = db.conn().execute(q, (self.only,) if self.only else ()).fetchall()
        conc = cfg()["collectors"]["techstack"]["concurrency"]

        def one(r):
            ev = probe(r["domain"])
            return Record(key=r["domain"], domain=r["domain"], name=r["name"],
                          body={"tools": detect(ev), "status_page": ev["status_page"]})

        for rec in fan_out([dict(r) for r in rows], one, conc):
            if rec:
                yield rec

    def signals(self, rec: Record, prev: dict | None):
        dom = rec.domain
        fps = cfg()["fingerprints"]
        for tool, how in rec.body["tools"].items():
            yield Sig("integration_detected", f"{dom}:integration:{tool}",
                      detail=f"{tool} ({how})",
                      payload={"tool": tool, "how": how,
                               "line": (fps.get(tool) or {}).get("line")})

        sp = rec.body.get("status_page")
        if sp:
            yield Sig("status_page", f"{dom}:status_page", detail=f"public status page: {sp}",
                      url=sp if sp.startswith("http") else "")
            # Appearing between sweeps is a fresh, timely trigger.
            if prev is not None and not prev.get("status_page"):
                yield Sig("new_status_page", f"{dom}:new_status_page:{db.now()[:10]}",
                          detail="Just published a status page — they now run their own production")
