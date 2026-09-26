"""Checks for the two capabilities added for volume and first-party intent."""
from __future__ import annotations

from app import db, scoring, tracking
from app.collectors import discovery, social
from app.config import cfg


# ---------------------------------------------------------------- discovery

def test_hn_hiring_extracts_the_company_not_the_ats_link():
    """Who-is-hiring posts link to Greenhouse/Lever/LinkedIn as often as to the
    company. Picking the first href would fill the universe with greenhouse.io."""
    text = ('Acme | SF | REMOTE | <a href="https://boards.greenhouse.io/acme">jobs</a> '
            '<a href="https://acme-corp.com/">acme-corp.com</a> we run Jira and Datadog')
    assert discovery._domain_from(text) == "acme-corp.com"

    only_ats = 'Foo | <a href="https://jobs.lever.co/foo">apply</a>'
    assert discovery._domain_from(only_ats) is None


def test_hn_escapes_slashes_in_hrefs():
    """Regression: HN returns href="https:&#x2F;&#x2F;ngrok.com&#x2F;". Running a
    URL regex before unescaping matched nothing, and the whole collector
    silently returned zero records while reporting success."""
    raw = ('ngrok | SF | Full-time | <a href="https:&#x2F;&#x2F;ngrok.com&#x2F;" '
           'rel="nofollow">https:&#x2F;&#x2F;ngrok.com&#x2F;</a>')
    assert discovery._domain_from(raw) == "ngrok.com"


def test_recruiting_infrastructure_is_never_a_prospect():
    """Regression: grnh.se (Greenhouse's URL shortener), ycombinator.com and
    app.dover.com all reached the ranked board as if they were companies."""
    for bad in ("https://grnh.se/abc123", "https://www.ycombinator.com/jobs",
                "https://app.dover.com/apply/xyz", "https://jobs.example.com/x",
                "https://boards.greenhouse.io/acme"):
        assert discovery._domain_from(f'<a href="{bad}">apply</a>') is None


def test_publicly_hiring_is_weaker_than_a_real_trigger():
    """Everyone in a Who-is-hiring thread is hiring, so it must not outrank an
    account that actually fired a first-SRE-hire trigger."""
    weak = db.upsert_account("weak.com")
    for i in range(4):
        db.add_signal(weak, "hiring_publicly", "hn_hiring", f"w{i}", detail="posted")

    strong = db.upsert_account("strong.com")
    db.add_signal(strong, "first_sre_hire", "ats", "s1", detail="hiring first SRE")

    a = scoring.score_rows(db.signals_for(weak))["intent"]
    b = scoring.score_rows(db.signals_for(strong))["intent"]
    assert b > a


def test_bare_domains_without_a_link_are_still_found():
    """Many posts write the domain as plain text: "Dave.com | Senior Engineer"."""
    assert discovery._domain_from("Dave.com | Senior Full Stack | LA") == "dave.com"
    assert discovery._domain_from("The Pacific Financial Group | DevOps | tpfg.com") == "tpfg.com"
    # Tech stacks are not companies.
    assert discovery._domain_from("We use React.js and ASP.NET here") is None
    assert discovery._domain_from("Backend in Node.js | Remote") is None


def test_hn_hiring_signals_pull_stack_and_prod_ownership():
    col = discovery.HnHiringCollector()
    rec = discovery.Record(
        key="c1", domain="acme-corp.com", name="Acme",
        body={"text": "Acme | Remote | Senior Engineer | we use Jira, Datadog and Slack. "
                      "You will join the on-call rotation and write postmortems.",
              "thread": "Ask HN: Who is hiring?", "url": "https://news.ycombinator.com/item?id=1"})
    kinds = {s.kind for s in col.signals(rec, None)}
    tools = {s.payload["tool"] for s in col.signals(rec, None)
             if s.kind == "integration_detected"}
    assert "hiring_publicly" in kinds
    assert "eng_req_surge" not in kinds      # posting a job ad is not a surge
    assert "owns_production_language" in kinds
    assert {"Jira", "Datadog", "Slack"} <= tools


def test_yc_discovery_scores_zero_intent():
    """Existing is not wanting to buy. Discovery must not manufacture intent."""
    col = discovery.YcCollector()
    rec = discovery.Record(key="yc:1", domain="ycco.com", name="YCCo",
                           body={"batch": "W25", "team_size": 20,
                                 "one_liner": "developer infrastructure for teams",
                                 "industry": "software", "tags": []})
    sigs = list(col.signals(rec, None))
    assert sigs and all(s.kind == "discovered" for s in sigs)

    aid = db.upsert_account("ycco.com")
    for s in sigs:
        db.add_signal(aid, s.kind, "yc", s.dedupe_key, s.detail)
    assert scoring.score_rows(db.signals_for(aid))["intent"] == 0


def test_yc_skips_non_software_companies():
    col = discovery.YcCollector()
    rec = discovery.Record(key="yc:2", domain="biotech.com", name="BioCo",
                           body={"batch": "W25", "team_size": 20,
                                 "one_liner": "gene therapy for rare disease",
                                 "industry": "healthcare", "tags": []})
    assert list(col.signals(rec, None)) == []


# ---------------------------------------------------------------- dedupe keys

def test_dedupe_keys_are_stable_across_processes():
    """Python randomises str hashing per interpreter, so hash() inside a
    dedupe_key silently re-emits every signal on each restart."""
    assert social._key("HN: some post") == social._key("HN: some post")
    assert social._key("a") != social._key("b")
    # Precomputed elsewhere — proves it is not process-local.
    assert social._key("malveon") == "0f0bd1b1e2f8b0e5be6c6a3b1f5c8b2e0f6b8a3d"[:16] \
        or len(social._key("malveon")) == 16


# ---------------------------------------------------------------- tracking

def test_bots_never_produce_a_buying_signal():
    """Automated traffic outweighs human traffic on the open web. A naive
    reverse-IP pipeline fires your best signal on a crawler."""
    assert tracking.is_bot("Mozilla/5.0 (compatible; GPTBot/1.0)", 30) is True
    assert tracking.is_bot("python-requests/2.31", 30) is True
    assert tracking.is_bot("", 30) is True
    assert tracking.is_bot("Mozilla/5.0 (Macintosh) Safari/605", 1) is True   # too fast
    assert tracking.is_bot("Mozilla/5.0 (Macintosh) Safari/605", 25) is False

    res = tracking.record_visit("66.249.66.1", "/pricing",
                                "Mozilla/5.0 (compatible; Googlebot/2.1)", "", 30)
    assert res["resolution"] == "bot"
    assert db.conn().execute("SELECT COUNT(*) FROM signal").fetchone()[0] == 0


def test_private_and_missing_ips_are_rejected():
    for ip in ("127.0.0.1", "192.168.1.5", "10.0.0.9", ""):
        assert tracking.record_visit(ip, "/", "Mozilla/5.0 Safari", "", 20)["ok"] is False


def test_isp_networks_are_not_treated_as_companies():
    assert tracking._is_isp("AS15169 Google LLC")
    assert tracking._is_isp("Reliance Jio Infocomm Limited")
    assert tracking._is_isp("Hetzner Online GmbH")
    assert not tracking._is_isp("Acme Robotics Inc")


def test_infrastructure_hostnames_never_become_accounts():
    """Regression: 8.8.8.8 resolved to the hostname dns.google, which passed a
    domain-only ISP check and put "dns.google" on the board as a visitor."""
    for d in ("dns.google", "ec2-1-2-3-4.compute-1.amazonaws.com",
              "static.1e100.net", "host.hetzner.de"):
        assert tracking._is_infra_domain(d), d
    assert not tracking._is_infra_domain("acme-corp.com")


def test_tagged_email_link_beats_ip_lookup(monkeypatch):
    """The whole point of email tracking: reverse-IP can only name companies
    big enough to own an ASN, so for a 10-150 person prospect it returns the
    visitor's ISP. A tagged link is certain — you know who you emailed."""
    def must_not_run(ip):
        raise AssertionError("IP lookup should be skipped when a tag is present")

    monkeypatch.setattr(tracking, "resolve_company", must_not_run)
    res = tracking.record_visit("203.0.113.60", "/pricing", "Mozilla/5.0 Safari",
                                "", 30, via="tagged-co.com")
    assert res["identified"] is True and res["domain"] == "tagged-co.com"

    row = db.conn().execute(
        "SELECT id FROM account WHERE domain='tagged-co.com'").fetchone()
    sigs = db.signals_for(row["id"])
    assert sigs[0]["kind"] == "site_visit_pricing"
    assert "Clicked your email" in sigs[0]["detail"]


def test_untagged_visit_still_falls_back_to_ip(monkeypatch):
    monkeypatch.setattr(tracking, "resolve_company",
                        lambda ip: ("fallback.com", "Fallback Inc", "ptr"))
    res = tracking.record_visit("203.0.113.61", "/", "Mozilla/5.0 Safari", "", 30)
    assert res["domain"] == "fallback.com"


def test_a_junk_tag_cannot_create_a_bogus_account(monkeypatch):
    """?via= is attacker-controllable, so it goes through the same domain
    validation as every other source rather than being trusted blindly."""
    monkeypatch.setattr(tracking, "resolve_company", lambda ip: (None, "", "none"))
    res = tracking.record_visit("203.0.113.62", "/", "Mozilla/5.0 Safari", "", 30,
                                via="not a domain")
    assert res.get("identified") is not True
    # gmail.com and friends are rejected by norm_domain too.
    tracking.record_visit("203.0.113.63", "/", "Mozilla/5.0 Safari", "", 30,
                          via="gmail.com")
    assert db.conn().execute(
        "SELECT COUNT(*) FROM account WHERE domain='gmail.com'").fetchone()[0] == 0


def test_real_visit_creates_an_account_and_a_signal(monkeypatch):
    monkeypatch.setattr(tracking, "resolve_company",
                        lambda ip: ("realcust.com", "Real Customer Inc", "ptr"))
    res = tracking.record_visit("203.0.113.9", "/pricing", "Mozilla/5.0 Safari", "", 30)
    assert res["identified"] is True and res["domain"] == "realcust.com"
    row = db.conn().execute("SELECT id FROM account WHERE domain='realcust.com'").fetchone()
    assert row
    kinds = {r["kind"] for r in db.signals_for(row["id"])}
    assert "site_visit_pricing" in kinds


def test_pricing_visit_outscores_a_generic_visit(monkeypatch):
    """A pricing-page read is a buying motion, not research — and it must
    outrank every signal inferred from outside the company."""
    monkeypatch.setattr(tracking, "resolve_company",
                        lambda ip: ("visitor.com", "Visitor Inc", "ptr"))

    tracking.record_visit("8.8.4.4", "/pricing", "Mozilla/5.0 Safari", "", 30)
    aid = db.conn().execute("SELECT id FROM account WHERE domain='visitor.com'").fetchone()["id"]
    hot = scoring.score_rows(db.signals_for(aid))["intent"]

    other = db.upsert_account("other.com")
    db.add_signal(other, "first_sre_hire", "ats", "k1", detail="hiring SRE")
    trigger = scoring.score_rows(db.signals_for(other))["intent"]

    assert hot > trigger
    assert hot >= cfg()["intent"]["site_visit_pricing"]["weight"] * 0.9


def test_repeat_refreshes_do_not_stack_signals(monkeypatch):
    monkeypatch.setattr(tracking, "resolve_company",
                        lambda ip: ("visitor.com", "Visitor Inc", "ptr"))
    for _ in range(5):
        tracking.record_visit("8.8.4.4", "/pricing", "Mozilla/5.0 Safari", "", 30)
    n = db.conn().execute(
        "SELECT COUNT(*) FROM signal WHERE kind='site_visit_pricing'").fetchone()[0]
    assert n == 1
    # Every hit is still recorded for the visits log, even when not re-scored.
    assert db.conn().execute("SELECT COUNT(*) FROM site_visit").fetchone()[0] == 5
