"""Product Hunt — daily launches of new software products.

Every other discovery source here finds companies that are already doing
something (hiring, raising, posting on HN). This one is different: it can
catch a company on the day it's born, before it has a job board, a status
page, or anything else this system looks for.

Needs a free developer token — producthunt.com > Settings > API Dashboard >
create an app > copy the "Developer Token". Takes about two minutes, no
approval wait. Off entirely without one (PRODUCTHUNT_TOKEN unset), same
pattern as Reddit in social.py.

Verified live once a token was added: the schema is right, but `website` is
NOT the product's own site — it's a producthunt.com/r/<code> click-tracking
redirect. Every domain came back as "producthunt.com" until this was caught,
so the redirect is followed to its real destination before recording it.
"""
from __future__ import annotations

import logging

from .. import db
from ..config import Env, cfg
from .base import Record, Sig, http
from .discovery import NOT_A_COMPANY

log = logging.getLogger("producthunt")

QUERY = """
query Discover($first: Int!) {
  posts(first: $first, order: NEWEST) {
    edges {
      node {
        id
        name
        tagline
        website
        url
        topics { edges { node { name } } }
      }
    }
  }
}
"""


def _real_domain(tracking_url: str | None) -> str | None:
    """`website` is a producthunt.com/r/<code> redirect, not the product's own
    site — this follows it (one extra request per launch, cheap and free) and
    normalizes whatever it actually lands on."""
    if not tracking_url:
        return None
    try:
        r = http.get(tracking_url)
    except Exception:
        return None
    final = str(r.url)
    # A redirect that didn't go anywhere (dead link) lands back on PH itself;
    # hobby-host defaults (vercel.app, github.io...) share the same filter
    # every other discovery source here uses.
    if NOT_A_COMPANY.search(final) or "producthunt.com" in final:
        return None
    return db.norm_domain(final)


class ProductHuntCollector:
    name = "producthunt"

    def fetch(self):
        spec = cfg()["discovery"]["producthunt"]
        if not spec.get("enabled") or not Env.PRODUCTHUNT_TOKEN:
            return
        try:
            d = http.post_json(
                "https://api.producthunt.com/v2/api/graphql",
                {"query": QUERY, "variables": {"first": spec["max_posts"]}},
                headers={"Authorization": f"Bearer {Env.PRODUCTHUNT_TOKEN}"})
        except Exception as exc:
            log.warning("producthunt: %s", exc)
            return
        if d.get("errors"):
            # A schema mismatch or bad token comes back as HTTP 200 with an
            # `errors` array (GraphQL convention), not an exception — without
            # this check it looks identical to "ran fine, found nothing".
            log.warning("producthunt API returned errors — token or schema "
                        "problem: %s", str(d["errors"])[:300])
            return
        edges = ((d.get("data") or {}).get("posts") or {}).get("edges") or []
        kept = 0
        for e in edges:
            n = e.get("node") or {}
            dom = _real_domain(n.get("website"))
            if not dom:
                continue
            topics = [t["node"]["name"] for t in ((n.get("topics") or {}).get("edges") or [])
                      if t.get("node")]
            kept += 1
            yield Record(key=f"ph:{n.get('id')}", domain=dom, name=n.get("name"),
                         body={"tagline": n.get("tagline") or "", "topics": topics,
                               "url": n.get("url") or ""})
        log.info("producthunt: %d launches matched a domain of %d", kept, len(edges))

    def signals(self, rec: Record, prev: dict | None):
        b = rec.body
        blob = f"{b['tagline']} {' '.join(b['topics'])}".lower()
        # Product Hunt launches everything from phone cases to SaaS. Same gate
        # as YcCollector — only count it if it looks like software.
        if not any(k in blob for k in ("software", "saas", "developer", "infrastructure",
                                       "devtool", "api", "platform", "b2b", "analytics",
                                       "security", "data", "ai", "cloud", "engineering")):
            return
        yield Sig("discovered", f"{rec.domain}:producthunt",
                  detail=f"Just launched on Product Hunt: {b['tagline'][:90]}",
                  url=b["url"])
