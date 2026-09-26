"""Checks for the seven sources added for signal breadth."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app import db, scoring
from app.collectors import ats, community, discovery, statuspage
from app.collectors.base import Record
from app.config import cfg


def _iso(days_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat(timespec="seconds")


# ---------------------------------------------------------------- statuspage

def _rec(incidents):
    return Record(key="acme.com", domain="acme.com", name="Acme",
                  body={"host": "status.acme.com", "incidents": incidents})


def test_major_incident_outranks_every_inferred_signal():
    """The whole point of this source: a company that broke prod last week beats
    a company that merely looks like an ICP fit."""
    col = statuspage.StatuspageCollector()
    sigs = list(col.signals(_rec([
        {"id": "1", "name": "Deployment Issues", "impact": "major",
         "status": "resolved", "created_at": _iso(5), "url": "https://s/1"}]), None))
    assert [s.kind for s in sigs] == ["major_incident"]

    hot = db.upsert_account("acme.com")
    for s in sigs:
        db.add_signal(hot, s.kind, "statuspage", s.dedupe_key, s.detail,
                      observed_at=s.observed_at)

    fit_only = db.upsert_account("fitonly.com")
    db.add_signal(fit_only, "first_sre_hire", "ats", "k", detail="hiring SRE",
                  observed_at=_iso(5))

    assert (scoring.score_rows(db.signals_for(hot))["intent"]
            > scoring.score_rows(db.signals_for(fit_only))["intent"])


def test_stale_incidents_are_ignored():
    col = statuspage.StatuspageCollector()
    old = list(col.signals(_rec([
        {"id": "9", "name": "Ancient outage", "impact": "major",
         "status": "resolved", "created_at": _iso(200), "url": ""}]), None))
    assert old == []


def test_repeated_incidents_fire_a_streak():
    col = statuspage.StatuspageCollector()
    inc = [{"id": str(i), "name": f"Outage {i}", "impact": "minor",
            "status": "resolved", "created_at": _iso(i + 1), "url": ""} for i in range(4)]
    kinds = [s.kind for s in col.signals(_rec(inc), None)]
    assert kinds.count("recent_incident") == 4
    assert "incident_streak" in kinds


def test_non_atlassian_status_pages_are_skipped_not_misparsed(monkeypatch):
    """Instatus and BetterStack serve an HTML app shell at the same path.
    Parsing that as JSON would either explode or silently yield nothing."""
    class R:
        status_code = 200
        headers = {"content-type": "text/html; charset=utf-8"}

        def json(self):
            raise AssertionError("must not attempt to parse HTML as JSON")

    monkeypatch.setattr(statuspage.http, "get", lambda url, **kw: R())
    assert statuspage.fetch_incidents("status.linear.app") is None


def test_recent_incidents_are_capped():
    """A noisy status page must not saturate intent on volume alone."""
    col = statuspage.StatuspageCollector()
    inc = [{"id": str(i), "name": f"Blip {i}", "impact": "minor",
            "status": "resolved", "created_at": _iso(1), "url": ""} for i in range(12)]
    aid = db.upsert_account("noisy.com")
    for s in col.signals(Record(key="noisy.com", domain="noisy.com",
                                body={"host": "s", "incidents": inc}), None):
        db.add_signal(aid, s.kind, "statuspage", s.dedupe_key, s.detail,
                      observed_at=s.observed_at)
    got = scoring.score_rows(db.signals_for(aid))
    cap = cfg()["intent"]["recent_incident"]["max"]
    streak = cfg()["intent"]["incident_streak"]["weight"]
    assert got["intent"] <= cap + streak + 1


# ---------------------------------------------------------------- community

def test_stargazer_signal_names_the_repo_being_evaluated():
    col = community.StargazerCollector()
    rec = Record(key="star", domain="acme.com", name="Acme",
                 body={"repo": "backstage/backstage", "login": "someone",
                       "when": _iso(3), "url": "https://github.com/someone"})
    s = list(col.signals(rec, None))[0]
    assert s.kind == "competitor_evaluation"
    assert "backstage/backstage" in s.detail


def test_community_post_with_persona_pain_beats_a_generic_one():
    col = community.CommunityCollector("mastodon")
    painful = Record(key="a", domain="a.com",
                     body={"label": "Mastodon #sre: root cause took 45 minutes again",
                           "persona": ("techlead", "root cause")})
    generic = Record(key="b", domain="b.com",
                     body={"label": "Mastodon #devops: thoughts on observability",
                           "persona": None})
    assert list(col.signals(painful, None))[0].kind == "persona_pain_post"
    assert list(col.signals(generic, None))[0].kind == "active_research"


# ---------------------------------------------------------------- vendors

def test_vendor_page_links_become_confirmed_integrations():
    col = discovery.VendorCustomerCollector()
    rec = Record(key="v", domain="customer.com", body={"tool": "Sentry",
                                                       "page": "https://sentry.io/customers/"})
    s = list(col.signals(rec, None))[0]
    assert s.kind == "integration_detected"
    assert s.payload["tool"] == "Sentry" and s.payload["line"] == "malviont"
    assert "customers page" in s.detail


def test_jobfeed_pulls_stack_and_persona_from_a_posting():
    col = discovery.JobFeedCollector()
    rec = Record(key="j", domain="acme.com", name="Acme",
                 body={"title": "Senior SRE", "url": "",
                       "text": "You will own on-call and cut incident triage. Stack: Datadog, Jira. "
                               "Today root cause takes 45 minutes."})
    sigs = list(col.signals(rec, None))
    kinds = {s.kind for s in sigs}
    tools = {s.payload["tool"] for s in sigs if s.kind == "integration_detected"}
    assert {"Datadog", "Jira"} <= tools
    assert "owns_production_language" in kinds
    assert any(s.payload.get("persona") == "techlead"
               for s in sigs if s.kind == "persona_pain_match")


def test_job_seeker_posts_are_never_ingested_as_companies():
    """Regression: "Location: Dakshina Kannada... Willing to relocate: Yes" with
    a drive.google.com résumé link became a ranked account. That is a person
    looking for work, filed as a prospect."""
    seeker = ('Location: Dakshina Kannada, Karnataka, India<p>Remote: Yes (IST overlap)'
              '<p>Willing to relocate: Yes<p>Technologies: Python, AWS'
              '<p><a href="https:&#x2F;&#x2F;drive.google.com&#x2F;file&#x2F;x">Resume</a>')
    assert discovery._is_seeker(seeker) is True

    hiring = ('Pathos AI | Senior Software / AI Engineer | NYC | Full-time | $180-200K'
              '<p>We run on-call and write postmortems.'
              '<p><a href="https:&#x2F;&#x2F;pathos.com&#x2F;">pathos.com</a>')
    assert discovery._is_seeker(hiring) is False
    # Résumé hosts must not survive even if the seeker check somehow misses.
    assert discovery._domain_from('<a href="https://drive.google.com/file/x">CV</a>') is None


def test_hiring_signal_names_the_actual_role():
    """"posted in Ask HN: Who is hiring? (May 2026)" gave you nothing to open an
    email with. The role is right there in the pipe-delimited header."""
    raw = "Pathos AI | Senior Software / AI Engineer | NYC (hybrid) | Full-time | $180-200K"
    assert discovery._role_from(raw) == "Senior Software / AI Engineer"

    col = discovery.HnHiringCollector()
    rec = Record(key="c1", domain="pathos.com", name="Pathos AI",
                 body={"text": raw + " We run on-call rotations and write postmortems.",
                       "thread": "Ask HN: Who is hiring? (May 2026)",
                       "role": "Senior Software / AI Engineer",
                       "url": "https://news.ycombinator.com/item?id=1"})
    by_kind = {s.kind: s.detail for s in col.signals(rec, None)}
    assert "Senior Software / AI Engineer" in by_kind["hiring_publicly"]
    # And the production evidence quotes their words rather than a category.
    assert "on-call" in by_kind["owns_production_language"]
    assert "mentions on-call / incident work" not in by_kind["owns_production_language"]


def test_first_release_hire_fires_once_like_the_other_role_triggers():
    """Mirrors first_sre_hire/first_em_hire: a company's first dedicated
    release/QA hire is the pre-deploy half of Malviont ("check the code
    before it's live"), not just outages caught after the fact."""
    col = ats.AtsCollector()
    job = {"id": "1", "title": "Release Engineer", "url": "https://x/1",
           "text": "", "dept": "", "posted": "2026-01-01T00:00:00+00:00"}
    rec = Record(key="acme.com", domain="acme.com", name="Acme",
                 body={"jobs": [job], "eng": 1, "total": 1})
    kinds = {s.kind for s in col.signals(rec, None)}
    assert "first_release_hire" in kinds

    # Same title already present last time -> it's not the first, must not refire.
    prev = {"jobs": [job], "eng": 1, "total": 1}
    kinds_again = {s.kind for s in col.signals(rec, prev)}
    assert "first_release_hire" not in kinds_again


def test_first_security_hire_is_a_hiring_signal_not_a_repo_scan():
    """The safer alternative to scanning code for leaked secrets: a company's
    first dedicated security/AppSec hire, via the same public job-post
    mechanism as every other role trigger — no repo access involved."""
    col = ats.AtsCollector()
    job = {"id": "1", "title": "Application Security Engineer", "url": "https://x/1",
           "text": "", "dept": "", "posted": "2026-01-01T00:00:00+00:00"}
    rec = Record(key="acme.com", domain="acme.com", name="Acme",
                 body={"jobs": [job], "eng": 1, "total": 1})
    assert "first_security_hire" in {s.kind for s in col.signals(rec, None)}


def test_board_reason_leads_with_the_most_quotable_evidence():
    """Aggregates score more points than a named role, but you cannot open an
    email with "3 Malveon integrations"."""
    aid = db.upsert_account("lead.com")
    for i, t in enumerate(["Slack", "AWS", "GitHub", "Jira"]):
        db.add_signal(aid, "integration_detected", "ats", f"i{i}",
                      detail=f"{t} named in job posts", payload={"tool": t})
    db.add_signal(aid, "persona_pain_post", "mastodon", "p1",
                  detail='Tech Lead pain — "root cause" · root cause took 45 minutes again',
                  observed_at=_iso(1), payload={"persona": "techlead"})
    why = scoring.explain(scoring.score_rows(db.signals_for(aid)))
    assert why.startswith("Tech Lead pain"), why


def test_every_intent_kind_belongs_to_a_lead_type():
    """Regression: supabase.com scored 100 — the highest in the system — with
    types=[], so it appeared under no filter chip at all. New signal kinds were
    scoring but were unclassified, making the best account invisible."""
    lt = cfg()["lead_types"]
    buckets = [set(lt["company_triggers"]), set(lt["intent_signals"]), set(lt["context_only"])]
    declared = set().union(*buckets)
    for kind in cfg()["intent"]:
        assert kind in declared, f"intent kind {kind!r} belongs to no lead type"
    for i, a in enumerate(buckets):
        for b in buckets[i + 1:]:
            assert not (a & b), f"kinds classified twice: {a & b}"


def test_a_common_signal_does_not_swamp_the_company_filter():
    """Regression: hiring_publicly fired on 481 of 895 accounts. Classified as a
    trigger it made more than half the universe a Company Lead, which is the
    same as having no filter."""
    from app import leads
    aid = db.upsert_account("justhiring.com")
    db.add_signal(aid, "hiring_publicly", "hn_hiring", "k1",
                  detail="posted in Who is hiring", observed_at=_iso(2))
    row = next(r for r in leads.board() if r["domain"] == "justhiring.com")
    assert "company" not in row["types"]
    assert scoring.score_rows(db.signals_for(aid))["intent"] > 0   # still scores


def test_an_incident_makes_it_a_company_lead():
    from app import leads
    aid = db.upsert_account("outage.com")
    db.add_signal(aid, "major_incident", "statuspage", "k1",
                  detail="major incident: prod down", observed_at=_iso(3))
    row = next(r for r in leads.board() if r["domain"] == "outage.com")
    assert "company" in row["types"]
    assert row["trigger"]


def test_a_pricing_page_visit_makes_it_an_intent_lead():
    from app import leads
    aid = db.upsert_account("shopper.com")
    db.add_signal(aid, "site_visit_pricing", "tracking", "k1",
                  detail="visited /pricing", observed_at=_iso(1))
    row = next(r for r in leads.board() if r["domain"] == "shopper.com")
    assert "intent" in row["types"]


def test_every_new_collector_is_wired_into_the_scheduler():
    """A collector that exists but is unreachable from the scheduler is dead
    code that still looks finished."""
    from app.scheduler import build
    for name in ("statuspage", "stargazers", "mastodon", "devto",
                 "vendors", "jobfeeds", "hn_hiring", "yc",
                 "show_hn", "wwr", "producthunt"):
        c = build(name)
        assert hasattr(c, "fetch") and hasattr(c, "signals") and c.name
        assert name in cfg()["collectors"], f"{name} missing from collectors config"
