"""Checks for the sources added on top of the seven: show_hn, wwr, producthunt."""
from __future__ import annotations

from app.collectors import discovery, producthunt
from app.collectors.base import Record
from app.config import Env


# ---------------------------------------------------------------- show_hn

def test_hobby_hosts_are_never_a_prospect():
    """A Show HN link to a demo video or a default free-tier subdomain names
    the platform, not the poster's company — same failure class as the
    recruiting-infrastructure regression this file's sibling already covers."""
    for bad in ("https://myproject.vercel.app", "https://someone.github.io/thing",
                "https://loom.com/share/abc123", "https://x.onrender.com",
                "https://handle.pages.dev"):
        assert discovery._domain_from(f'<a href="{bad}">demo</a>') is None


def test_show_hn_signals_are_discovery_only():
    """Existing is not wanting to buy — same rule as YcCollector."""
    col = discovery.ShowHnCollector()
    rec = Record(key="showhn:1", domain="realstartup.com",
                 body={"title": "A devtool for platform teams",
                       "url": "https://news.ycombinator.com/item?id=1"})
    sigs = list(col.signals(rec, None))
    assert sigs and all(s.kind == "discovered" for s in sigs)


# ---------------------------------------------------------------- wwr

RSS_SAMPLE = """<?xml version="1.0"?>
<rss><channel>
<item>
<title>Acme Corp: Senior Backend Engineer</title>
<region>Anywhere</region>
<category>Full-Stack Programming</category>
<description>
&lt;p&gt;We run on-call and write postmortems. Stack: Datadog, Jira.&lt;/p&gt;
&lt;p&gt;More at &lt;a href="https://acme-corp.com/careers"&gt;acme-corp.com&lt;/a&gt;&lt;/p&gt;
</description>
</item>
<item>
<title>No colon here so this row must be skipped</title>
<description>&lt;p&gt;&lt;a href="https://ignored.com"&gt;ignored.com&lt;/a&gt;&lt;/p&gt;</description>
</item>
</channel></rss>"""


class _Resp:
    def __init__(self, text, status_code=200):
        self.text, self.status_code = text, status_code


def test_wwr_extracts_company_domain_and_role_from_rss(monkeypatch):
    monkeypatch.setattr(discovery.http, "get", lambda url, **kw: _Resp(RSS_SAMPLE))
    col = discovery.WeWorkRemotelyCollector()
    recs = list(col.fetch())
    assert recs
    r = recs[0]
    assert r.domain == "acme-corp.com"
    assert r.name == "Acme Corp"
    assert r.body["role"] == "Senior Backend Engineer"
    # The malformed second item (no "Company: Role" colon) must not leak through.
    assert all(rec.domain != "ignored.com" for rec in recs)


def test_wwr_signals_pull_hiring_and_stack_evidence():
    col = discovery.WeWorkRemotelyCollector()
    rec = Record(key="wwr:1", domain="acme-corp.com", name="Acme Corp",
                 body={"role": "Senior SRE", "url": "https://weworkremotely.com/categories/x",
                       "text": "We run on-call and write postmortems. Stack: Datadog, Jira."})
    sigs = list(col.signals(rec, None))
    kinds = {s.kind for s in sigs}
    tools = {s.payload["tool"] for s in sigs if s.kind == "integration_detected"}
    assert "hiring_publicly" in kinds
    assert "owns_production_language" in kinds
    assert {"Datadog", "Jira"} <= tools


# ---------------------------------------------------------------- producthunt

def test_producthunt_does_nothing_without_a_token(monkeypatch):
    """No signup, no key entered — must not attempt a request at all."""
    monkeypatch.setattr(Env, "PRODUCTHUNT_TOKEN", "")

    def must_not_run(*a, **kw):
        raise AssertionError("must not call the API without a token")
    monkeypatch.setattr(producthunt.http, "post_json", must_not_run)
    assert list(producthunt.ProductHuntCollector().fetch()) == []


def test_producthunt_graphql_errors_are_not_reported_as_a_clean_zero(monkeypatch, caplog):
    """A bad token or a schema drift comes back as HTTP 200 with an `errors`
    array (GraphQL convention) — silently treating that as "0 records, ok"
    would hide a broken collector forever."""
    monkeypatch.setattr(Env, "PRODUCTHUNT_TOKEN", "fake-token")
    monkeypatch.setattr(producthunt.http, "post_json",
                        lambda *a, **kw: {"errors": [{"message": "field 'website' not found"}]})
    with caplog.at_level("WARNING"):
        assert list(producthunt.ProductHuntCollector().fetch()) == []
    assert any("errors" in r.message for r in caplog.records)


class _RedirectResp:
    def __init__(self, url):
        self.url = url


def test_producthunt_follows_the_tracking_redirect_to_the_real_site(monkeypatch):
    """Regression: `website` is a producthunt.com/r/<code> click-tracking
    link, not the product's own domain — verified live, every launch came
    back as "producthunt.com" until the redirect was actually followed."""
    monkeypatch.setattr(producthunt.http, "get",
                        lambda url, **kw: _RedirectResp("https://realproduct.com/?ref=producthunt"))
    assert producthunt._real_domain("https://www.producthunt.com/r/ABC123") == "realproduct.com"

    # A dead link that never redirects lands back on PH itself — must not
    # create an account for Product Hunt.
    monkeypatch.setattr(producthunt.http, "get",
                        lambda url, **kw: _RedirectResp("https://www.producthunt.com/r/ABC123"))
    assert producthunt._real_domain("https://www.producthunt.com/r/ABC123") is None

    assert producthunt._real_domain(None) is None


def test_producthunt_skips_non_software_launches():
    col = producthunt.ProductHuntCollector()
    rec = Record(key="ph:1", domain="physicalgadget.com",
                 body={"tagline": "A better phone case", "topics": ["Gadgets"], "url": ""})
    assert list(col.signals(rec, None)) == []

    rec2 = Record(key="ph:2", domain="devtool.com",
                  body={"tagline": "Ship faster with our API platform",
                        "topics": ["Developer Tools", "SaaS"], "url": "https://producthunt.com/x"})
    sigs = list(col.signals(rec2, None))
    assert sigs and sigs[0].kind == "discovered"
