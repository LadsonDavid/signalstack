"""Six checks over the logic that would silently corrupt the board if it broke."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from app import contacts, db, leads, scoring
from app.collectors import base
from app.collectors.ats import AtsCollector
from app.config import cfg


def _ago(days: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")


# 1 -------------------------------------------------------------- idempotency
def test_signals_are_idempotent():
    """Re-running a collector must never double-count. The guard is the UNIQUE
    constraint on dedupe_key, so this is the check that it is actually wired."""
    aid = db.upsert_account("acme.com")
    for _ in range(3):
        db.add_signal(aid, "first_sre_hire", "ats", "acme.com:first_sre_hire:99", detail="x")
    rows = db.signals_for(aid)
    assert len(rows) == 1

    first = db.add_signal(aid, "first_em_hire", "ats", "acme.com:em:1")
    again = db.add_signal(aid, "first_em_hire", "ats", "acme.com:em:1")
    assert first is True and again is False


def test_a_domains_name_is_set_once_and_never_overwritten():
    """Regression: a noisy discovery post ("Fluidstack ... <link to
    anthropic.com>") called upsert_account("anthropic.com", name="Fluidstack")
    and silently renamed the real Anthropic account — upsert_account always
    preferred whatever name arrived most recently. A domain is one company
    forever, so the first real name must stick regardless of what a later,
    noisier source guesses."""
    aid = db.upsert_account("real-name.com", name="Real Co")
    same = db.upsert_account("real-name.com", name="Wrong Guess Inc")
    assert aid == same
    assert db.conn().execute(
        "SELECT name FROM account WHERE id=?", (aid,)).fetchone()["name"] == "Real Co"

    # A domain seen with no name yet must still accept the first real one.
    aid2 = db.upsert_account("blank-first.com")
    db.upsert_account("blank-first.com", name="First Real Name")
    assert db.conn().execute(
        "SELECT name FROM account WHERE id=?", (aid2,)).fetchone()["name"] == "First Real Name"

    # Other fields are unaffected — they keep preferring the newest value.
    db.upsert_account("real-name.com", headcount=50)
    db.upsert_account("real-name.com", headcount=80)
    assert db.conn().execute(
        "SELECT headcount FROM account WHERE id=?", (aid,)).fetchone()["headcount"] == 80


# 2 ------------------------------------------------------------------- decay
def test_decay_halves_at_the_half_life():
    assert scoring.decay(40, 0, 10) == pytest.approx(40)
    assert scoring.decay(40, 10, 10) == pytest.approx(20)
    assert scoring.decay(40, 20, 10) == pytest.approx(10)


def test_old_intent_decays_out_of_the_score():
    """The whole reason there is no decay cron: age is applied at read time."""
    aid = db.upsert_account("decay.com")
    hl = cfg()["intent"]["stated_intent"]["half_life_days"]
    db.add_signal(aid, "stated_intent", "llm", "k1", detail="asked for a tool",
                  observed_at=_ago(0))
    fresh = scoring.score_rows(db.signals_for(aid))["intent"]

    with db.tx() as c:
        c.execute("UPDATE signal SET observed_at=? WHERE dedupe_key='k1'", (_ago(hl * 4),))
    stale = scoring.score_rows(db.signals_for(aid))["intent"]

    assert fresh > 40
    assert stale < fresh / 8


# 3 ------------------------------------------------------------ fit + gating
def _tools(aid, tools):
    for i, t in enumerate(tools):
        db.add_signal(aid, "integration_detected", "techstack", f"{aid}:k{i}",
                      detail=f"{t} (header)", payload={"tool": t})


def test_integration_weighting_beats_counting():
    """Every dev-infra company runs AWS+K8s+GitHub. If integrations were merely
    counted, commodity infra would score the same as real tool sprawl and the
    whole board would flatten to fit=100."""
    commodity = db.upsert_account("commodity.com")
    _tools(commodity, ["AWS", "Azure", "GCP", "Kubernetes", "GitHub"])

    sprawl = db.upsert_account("sprawl.com")
    _tools(sprawl, ["Slack", "Jira", "Linear", "PagerDuty", "Sentry"])

    a = scoring.score_rows(db.signals_for(commodity))
    b = scoring.score_rows(db.signals_for(sprawl))
    assert len(a["tools"]) == len(b["tools"]) == 5
    assert b["fit"] > a["fit"] * 3          # same count, very different meaning


def test_multiword_tool_names_survive():
    """Regression: recovering the tool by splitting detail on space turned
    'Google Docs' into 'Google' and 'Microsoft Teams' into 'Microsoft'."""
    aid = db.upsert_account("multi.com")
    for i, t in enumerate(["Google Docs", "Microsoft Teams"]):
        db.add_signal(aid, "integration_detected", "ats", f"m{i}",
                      detail=f"{t} named in job posts", payload={"tool": t})
    assert scoring.score_rows(db.signals_for(aid))["tools"] == ["Google Docs", "Microsoft Teams"]


def test_fit_caps_and_zero_intent_means_zero_score():
    aid = db.upsert_account("fit.com")
    _tools(aid, ["Sentry", "Datadog", "Linear", "Jira", "Slack", "PagerDuty",
                 "Notion", "Confluence"])
    db.add_signal(aid, "status_page", "techstack", "sp", detail="status page")
    sc = scoring.score_rows(db.signals_for(aid))

    F = cfg()["fit"]
    expected = (F["integration_detected"]["cap"] + F["status_page"]
                + F["multi_line_bonus"]["three_lines"])
    assert sc["fit"] == expected     # weights exceed the cap, plus 3-line sprawl
    assert sc["intent"] == 0
    assert sc["score"] == 0        # perfect fit, zero intent -> not today's problem


def test_sprawl_across_product_lines_beats_depth_in_one():
    """Malveon exists for sprawl ACROSS categories. Four Malviont tools is one
    problem; one tool from each line is the problem Malveon actually solves."""
    deep = db.upsert_account("deep.com")
    _tools(deep, ["Sentry", "Datadog", "PagerDuty", "Vercel"])      # all malviont

    wide = db.upsert_account("wide.com")
    _tools(wide, ["Sentry", "Jira", "Slack", "Notion"])             # three lines

    a = scoring.score_rows(db.signals_for(deep))
    b = scoring.score_rows(db.signals_for(wide))
    assert len(a["lines"]) == 1 and len(b["lines"]) == 3
    assert b["fit"] > a["fit"]
    assert b["best_line"] == "malve"          # two of four tools are Context
    assert a["best_line"] == "malviont"


def test_persona_pain_in_their_own_job_ad():
    """A company writing a persona's villain into its own JD has told you which
    product line to lead with."""
    col = AtsCollector()
    jd = {"text": "You will cut incident triage: today root cause takes 45 minutes "
                  "across six tabs. On-call and postmortem culture matter here.",
          "title": "Staff Engineer", "dept": "Platform", "id": "1", "url": "", "posted": None}
    rec = base.Record(key="k", domain="painco.com", name="PainCo",
                      body={"jobs": [jd], "eng": 1, "total": 1})
    sigs = list(col.signals(rec, None))
    pain = [s for s in sigs if s.kind == "persona_pain_match"]
    assert pain and pain[0].payload["persona"] == "techlead"
    assert pain[0].payload["line"] == "malviont"

    aid = db.upsert_account("painco.com")
    for s in sigs:
        db.add_signal(aid, s.kind, "ats", s.dedupe_key, s.detail, payload=s.payload)
    assert "techlead" in scoring.score_rows(db.signals_for(aid))["personas"]


def test_competitors_are_never_prospects():
    aid = db.upsert_account("incident.io")
    _tools(aid, ["Slack", "Jira", "PagerDuty"])
    assert "incident.io" not in {r["domain"] for r in leads.board()}


def test_disqualifier_blocks_an_agency():
    col = AtsCollector()
    rec = base.Record(key="d", domain="bigconsulting.com", name="Big Consulting",
                      body={"jobs": [{"text": "on-call", "title": "SRE", "dept": "Eng",
                                      "id": "1", "url": "", "posted": None}]})
    assert list(col.signals(rec, None)) == []
    row = db.conn().execute("SELECT disqualified FROM account WHERE domain=?",
                            ("bigconsulting.com",)).fetchone()
    assert row and "consulting" in row["disqualified"]
    assert leads.board() == []      # disqualified accounts never reach the board


# 4 ------------------------------------------------------- email waterfall
def test_waterfall_short_circuits_before_the_paid_call(monkeypatch):
    """Every rejection below must cost zero credits. If this breaks, the bill does."""
    def boom(*a, **k):
        raise AssertionError("paid verifier was called")

    monkeypatch.setattr(contacts.http, "json", boom)
    monkeypatch.setattr(contacts.Env, "MILLIONVERIFIER_KEY", "fake-key")
    monkeypatch.setattr(contacts, "mx_ok", lambda d: False)

    assert contacts.verify("not-an-email")["status"] == "invalid"
    assert contacts.verify("sales@acme.com")["reason"] == "role address"
    assert contacts.verify("bob@gmail.com")["reason"] == "freemail"
    assert contacts.verify("bob@mailinator.com")["reason"] == "disposable"
    assert contacts.verify("real.person@nomx.com")["reason"] == "no MX record"


def test_pattern_inference_learns_the_house_format():
    """One real address scraped from public commit history tells you how to
    construct the CTO's, which is the difference between a first-try hit and a
    bounce that costs domain reputation."""
    aid = db.upsert_account("known.com")
    with db.tx() as c:
        c.execute("INSERT INTO contact(account_id,name,email,status,source,created_at) "
                  "VALUES(?,?,?,?,?,?)",
                  (aid, "Liz Fong", "lizf@known.com", "valid", "github:commits", db.now()))

    # {f}{li} must be learned from the example, not the {f}.{l} default.
    assert contacts.house_pattern("known.com") == "{f}{li}"
    assert contacts.guess_emails("Sam", "Smith", "known.com")[0] == "sams@known.com"


def test_service_accounts_are_never_treated_as_people():
    """Regression: accounts+githubbot@langfuse.com reached the board as a
    contact. A CI robot's address is a wasted send and instant credibility loss."""
    for bad in ("accounts+githubbot@x.com", "github-actions[bot]@x.com",
                "ci@x.com", "dependabot@x.com", "build+bot@x.com",
                "langfuse-bot@langfuse.com", "deploy.bot@x.com",
                "1234+user@users.noreply.github.com"):
        assert contacts.NOREPLY.search(bad), bad
    for good in ("lizf@honeycomb.io", "aaron@tailscale.com", "ant.wilson@supabase.com"):
        assert not contacts.NOREPLY.search(good), good


def test_board_shows_the_decision_maker_first():
    aid = db.upsert_account("rank-contacts.com")
    rows = [("junior@x.com", "Junior Developer", "pattern_confirmed"),
            ("boss@x.com", "VP of Engineering", "pattern_confirmed"),
            ("guess@x.com", "CTO", "guessed")]
    with db.tx() as c:
        for email, title, status in rows:
            c.execute("INSERT INTO contact(account_id,name,title,email,status,source,created_at)"
                      " VALUES(?,?,?,?,?,?,?)",
                      (aid, "N", title, email, status, "t", db.now()))
    got = next(r for r in leads.board() if r["domain"] == "rank-contacts.com")["contacts"]
    # Confirmed beats guessed; within confirmed, seniority wins over alphabetical.
    assert got[0]["email"] == "boss@x.com"
    assert got[-1]["email"] == "guess@x.com"


def test_invalid_contacts_never_fill_the_who_to_email_slot():
    """Regression: a manually-checked throwaway address ("test@..." typed into
    the verify box) came back invalid and was the ONLY contact stored for the
    account, so it became the board's "who to email" — a known-bad address is
    worse than showing none at all."""
    aid = db.upsert_account("bad-contact-only.com")
    with db.tx() as c:
        c.execute("INSERT INTO contact(account_id,name,email,status,source,created_at)"
                  " VALUES(?,?,?,?,?,?)",
                  (aid, "", "test@railway-verify-check.com", "invalid", "manual", db.now()))
    row = next(r for r in leads.board() if r["domain"] == "bad-contact-only.com")
    assert row["contacts"] == []


def test_derive_pattern_handles_hyphenated_surnames():
    assert contacts.derive_pattern("Liz", "Fong-Jones", "lizf") == "{f}{li}"
    assert contacts.derive_pattern("Robb", "Kidd", "robbkidd") == "{f}{l}"
    assert contacts.derive_pattern("Jane", "Doe", "j.doe") == "{fi}.{l}"
    assert contacts.derive_pattern("Jane", "Doe", "totally-unrelated") is None


# 5 ------------------------------------------------------------ circuit breaker
def test_breaker_opens_after_threshold_and_resets():
    name = "flaky"
    threshold = cfg()["breaker"]["failure_threshold"]
    for _ in range(threshold):
        base._breaker_trip(name)
    assert base._breaker_open(name) is True

    base._breaker_reset(name)
    assert base._breaker_open(name) is False


def test_failing_collector_does_not_raise_and_is_logged():
    class Broken:
        name = "broken"

        def fetch(self):
            raise RuntimeError("upstream is down")

        def signals(self, rec, prev):
            return []

    res = base.run_collector(Broken())
    assert res["status"] == "failed"
    row = db.conn().execute(
        "SELECT status,error FROM collector_run WHERE collector='broken'").fetchone()
    assert row["status"] == "failed" and "upstream" in row["error"]


# --------------------------------------------------------- cross-layer pain
def test_pain_across_two_product_lines_scores_higher_than_either_alone():
    """No tracked competitor spans context + planning + execution — that
    breadth is Malveon's whole thesis. A company hurting in two product
    areas at once should outscore the same single signal alone."""
    both = db.upsert_account("bothpain.com")
    db.add_signal(both, "major_incident", "statuspage", "b1", detail="prod down",
                  observed_at=_ago(2))
    db.add_signal(both, "persona_pain_post", "hn", "b2",
                  detail="CEO pain — \"plan vs built\"",
                  payload={"persona": "ceo", "term": "plan vs built"}, observed_at=_ago(2))

    one = db.upsert_account("onepain.com")
    db.add_signal(one, "major_incident", "statuspage", "o1", detail="prod down",
                  observed_at=_ago(2))

    both_sc = scoring.score_rows(db.signals_for(both))
    one_sc = scoring.score_rows(db.signals_for(one))
    assert both_sc["intent"] > one_sc["intent"]
    assert any(r["kind"] == "multi_layer_pain" for r in both_sc["reasons"])
    assert not any(r["kind"] == "multi_layer_pain" for r in one_sc["reasons"])


# 6 --------------------------------------------------------- lead partitioning
def test_lead_types_partition_as_expected():
    win = cfg()["thresholds"]["trigger_window_days"]

    company = db.upsert_account("company.com")
    db.add_signal(company, "first_sre_hire", "ats", "c1", detail="hiring SRE", observed_at=_ago(2))

    intent = db.upsert_account("intent.com")
    db.add_signal(intent, "stated_intent", "llm", "i1", detail="asked for a tool", observed_at=_ago(1))

    cold = db.upsert_account("cold.com")
    for i, t in enumerate(["Sentry", "Vercel", "Datadog", "Linear", "Jira"]):
        db.add_signal(cold, "integration_detected", "techstack", f"x{i}", detail=f"{t} (header)")

    stale = db.upsert_account("stale.com")
    db.add_signal(stale, "first_sre_hire", "ats", "s1", detail="old", observed_at=_ago(win + 30))

    doms = lambda t: {r["domain"] for r in leads.board(lead_type=t)}
    assert "company.com" in doms("company")
    assert "stale.com" not in doms("company")        # outside the trigger window
    assert "intent.com" in doms("intent")
    assert "cold.com" in doms("cold")
    assert "company.com" not in doms("cold")         # live intent -> not cold

    top = leads.board()
    assert top[0]["domain"] in {"intent.com", "company.com"}
    assert top[0]["why"]                              # every row explains itself


def test_pain_points_lists_only_companies_with_a_real_human_signal_and_shows_hosting():
    """Separate from the board — a company must never show up here purely for
    an outage or a detected tool. Only a real persona/research/gripe/intent
    signal earns a spot, and hosting is called out when it's known."""
    voiced = db.upsert_account("voiced.com")
    db.add_signal(voiced, "persona_pain_post", "hn", "v1",
                  detail="Tech Lead pain — \"root cause\"",
                  payload={"persona": "techlead", "term": "root cause"}, observed_at=_ago(1))
    db.add_signal(voiced, "integration_detected", "techstack", "v2", detail="Vercel (header x-vercel-id)",
                  payload={"tool": "Vercel", "how": "header x-vercel-id"})

    silent = db.upsert_account("silent.com")
    db.add_signal(silent, "major_incident", "statuspage", "s1", detail="prod down", observed_at=_ago(1))

    rows = leads.pain_points()
    by_domain = {r["domain"]: r for r in rows}
    assert "voiced.com" in by_domain
    assert "silent.com" not in by_domain
    assert by_domain["voiced.com"]["hosting"] == ["Vercel"]
    assert by_domain["voiced.com"]["quotes"][0]["kind"] == "persona_pain_post"


def test_hosting_ignores_a_job_ad_mention_of_the_same_tool():
    """Regression: AWS/Azure/GCP used to be detected ONLY from job-ad text
    ("experience with AWS a plus"), so one generic sentence lit up all three
    at once and made it look like a company ran on four clouds simultaneously.
    A job-ad mention must never count as hosting — only techstack's own probe,
    which reads a real header/CNAME the company's own server sent, can."""
    aid = db.upsert_account("jobad-only.com")
    db.add_signal(aid, "integration_detected", "ats", "j1", detail="AWS named in job posts",
                  payload={"tool": "AWS"})
    assert leads._hosting_for(db.signals_for(aid)) == []


# ------------------------------------------------------------------ misc
def test_prefilter_rejects_bots_and_substring_false_positives():
    """Regression: substring matching fired "adr" inside "quadratic" and the
    GitHub feed filled with dependabot PRs, drowning the real posts."""
    from app.collectors import social

    assert social._prefilter("how do you run a postmortem review")
    assert social._prefilter("our ADR process is a mess")
    assert not social._prefilter("solving the quadratic formula in hadron physics")
    assert not social._prefilter("Rust 1.94 released with const generics")

    idx = []
    assert social._store("github", "1", "dependabot[bot]", "", "chore(deps): bump x",
                         "on-call postmortem runbook", "", idx) is None
    assert social._store("github", "2", "realuser", "",
                         "Bump golang.org/x/net", "on-call postmortem", "", idx) is None
    assert db.conn().execute("SELECT COUNT(*) FROM intent_post").fetchone()[0] == 0


def test_vendor_is_not_credited_with_running_itself():
    """Regression: honeycomb.io named itself in its own job posts and was
    credited "already runs honeycomb — pain and budget both exist"."""
    col = AtsCollector()
    jd = {"text": "we run honeycomb and datadog in production", "title": "SRE",
          "dept": "Eng", "id": "1", "url": "", "posted": None}
    rec = base.Record(key="k", domain="honeycomb.io", name="Honeycomb",
                      body={"jobs": [jd], "eng": 1, "total": 1})
    kinds = [(s.kind, s.detail) for s in col.signals(rec, None)]
    comp = [d for k, d in kinds if k == "competitor_complementary"]
    assert not any("honeycomb" in d for d in comp)
    assert any("datadog" in d for d in comp)      # genuine third-party tool still counts


def test_opening_line_leads_with_the_predictive_tools():
    aid = db.upsert_account("rank.com")
    _tools(aid, ["AWS", "Azure", "PagerDuty", "Slack"])
    ranked = scoring.score_rows(db.signals_for(aid))["tools_ranked"]
    assert ranked[:2] == ["PagerDuty", "Slack"]   # not the alphabetical AWS, Azure


def test_account_matching_never_fires_on_a_bare_domain_label():
    """Regression: matching domain.split('.')[0] made trigger.dev match every
    GitHub issue containing the word "trigger" and pushed it to intent=100."""
    from app.collectors import social

    db.upsert_account("trigger.dev")
    db.upsert_account("val.town")
    idx = social._account_index()
    noise = "fix(catalog-react): EntityOwnerPicker crashes when a trigger has no val"
    assert not any(pat.search(noise) for pat, _, _ in idx)

    real = "we run trigger.dev in production and need better on-call context"
    assert any(pat.search(real) for pat, _, _ in idx)


def test_eval_set_is_wellformed():
    """The eval set is the only thing standing between you and a classifier that
    quietly returns garbage, so it must at least parse and cover every label."""
    import json
    import pathlib

    from app import llm
    p = pathlib.Path(__file__).resolve().parent.parent / "eval" / "intent_labels.json"
    cases = json.loads(p.read_text(encoding="utf-8"))
    assert len(cases) >= 40
    assert {c["label"] for c in cases} == set(llm.LABELS)
    assert all(c["text"].strip() for c in cases)


def test_domain_normalisation():
    assert db.norm_domain("https://WWW.Acme.com/careers?x=1") == "acme.com"
    assert db.norm_domain("bob@acme.com") == "acme.com"
    assert db.norm_domain("gmail.com") is None
    assert db.norm_domain("not a domain") is None


def test_board_does_not_repeat_the_trigger_in_the_reason_line():
    """The board shows the newest trigger on its own ⚡ line. Repeating it
    verbatim as the first reason wasted the most valuable space on the row."""
    aid = db.upsert_account("dedupe-why.com")
    db.add_signal(aid, "incident_streak", "statuspage", "s1",
                  detail="21 outages in the past month", observed_at=_ago(1))
    db.add_signal(aid, "integration_detected", "techstack", "s2",
                  detail="Slack (in page source)", payload={"tool": "Slack"})

    row = next(r for r in leads.board() if r["domain"] == "dedupe-why.com")
    assert row["trigger"] == "21 outages in the past month"
    assert "21 outages in the past month" not in row["why"]
    assert row["why"]  # still explains something


def test_orphaned_running_rows_are_reconciled_at_startup():
    """Regression: a collector_run row is only ever "running" while the
    process that started it is alive. If that process is killed mid-run
    (crash, redeploy, manual restart), nothing inside it ever runs to
    finalize the row, so it sat as "running" forever and made the Ops page's
    recent-runs list permanently misleading."""
    with db.tx() as c:
        c.execute("INSERT INTO collector_run(collector,started_at,status) "
                  "VALUES(?,?,?)", ("ats", db.now(), "running"))

    db._reconciled = False  # simulate an actual new process, not a second call in this one
    db.init()  # the only point where a prior process incarnation is provably gone

    row = db.conn().execute(
        "SELECT status, finished_at, error FROM collector_run WHERE collector='ats'").fetchone()
    assert row["status"] == "failed"
    assert row["finished_at"] is not None
    assert "restart" in row["error"]


def test_a_genuinely_running_row_from_this_process_is_left_alone():
    """init() must only clean up rows already 'running' when it starts, not
    something a still-alive process legitimately has in flight."""
    with db.tx() as c:
        c.execute("INSERT INTO collector_run(collector,started_at,status) "
                  "VALUES(?,?,?)", ("edgar", db.now(), "ok"))
    db.init()
    assert db.conn().execute(
        "SELECT status FROM collector_run WHERE collector='edgar'").fetchone()["status"] == "ok"


def test_a_concurrently_running_collector_survives_another_ones_init_call():
    """Regression: scheduler.run_one() calls db.init() before every single
    collector run, not just at boot. Two collectors run on independent
    schedules and can genuinely overlap — with reconciliation not guarded,
    collector B's init() call would mark collector A's still-in-flight row
    "orphaned" out from under it, purely because A happened to still be
    running when B's turn came up. A's real completion overwrites the status
    moments later, but the false error text was already live on /ops."""
    with db.tx() as c:
        c.execute("INSERT INTO collector_run(collector,started_at,status) "
                  "VALUES(?,?,?)", ("stargazers", db.now(), "running"))

    db.init()  # a second collector's run_one() firing mid-way through the first

    row = db.conn().execute(
        "SELECT status, error FROM collector_run WHERE collector='stargazers'").fetchone()
    assert row["status"] == "running"
    assert row["error"] is None


def test_init_purges_signals_fired_from_now_removed_generic_pain_terms():
    """Pruning a too-generic term (e.g. "root cause") from malveon.yaml only
    stops NEW false matches — signals already fired from it stay in the
    append-only log forever unless something explicitly cleans them up."""
    aid = db.upsert_account("stale-pain.com")
    db.add_signal(aid, "persona_pain_match", "ats", "sp1",
                  detail="Tech Lead problem: \"root cause\"",
                  payload={"persona": "techlead", "term": "root cause"})
    db.add_signal(aid, "persona_pain_post", "hn", "sp2",
                  detail="EM pain — \"capacity planning\"",
                  payload={"persona": "em", "term": "capacity planning"})
    real = db.add_signal(aid, "persona_pain_match", "ats", "sp3",
                         detail="Tech Lead problem: \"six tabs\"",
                         payload={"persona": "techlead", "term": "six tabs"})
    assert real is True

    db.init()

    kinds_left = [dict(r) for r in db.signals_for(aid)]
    terms_left = {json.loads(r["payload"] or "{}").get("term") for r in kinds_left}
    assert "root cause" not in terms_left
    assert "capacity planning" not in terms_left
    assert "six tabs" in terms_left    # a real, still-valid term must survive


def test_find_terms_needs_a_word_boundary_for_short_terms():
    """Regression: plain substring matching let "sla" fire inside "Slack" and
    "slo" fire inside "slow" — every company that merely mentioned using
    Slack got credited with "their job ads say: sla"."""
    assert base.find_terms(["sla"], "we use slack and jira daily") == []
    assert base.find_terms(["sla"], "our sla is 99.9% uptime") == ["sla"]
    assert base.find_terms(["slo"], "we move slow sometimes") == []
    assert base.find_terms(["slo"], "we define an slo for latency") == ["slo"]
    # Longer/multi-word terms never needed a boundary to be safe.
    assert base.find_terms(["root cause analysis"],
                           "run a root cause analysis") == ["root cause analysis"]


def test_owns_production_language_ignores_sla_inside_slack():
    col = AtsCollector()
    jd = {"text": "Join our team! We use Slack and Jira for collaboration.",
          "title": "Software Engineer", "dept": "", "id": "1", "url": "", "posted": None}
    rec = base.Record(key="k", domain="noprod.com", name="NoProd",
                      body={"jobs": [jd], "eng": 1, "total": 1})
    sigs = list(col.signals(rec, None))
    assert not any(s.kind == "owns_production_language" for s in sigs)


def test_owns_production_language_is_purged_and_regenerates_clean():
    """Its detail bundles every hit into one string per account, so a false
    "sla"-inside-"Slack" match couldn't be surgically removed — the whole
    signal has to be wiped and left to regenerate correctly."""
    aid = db.upsert_account("purge-check.com")
    db.add_signal(aid, "owns_production_language", "ats", "purge-check.com:owns_prod",
                  detail="their job ads say: “sla”")
    db.init()
    assert db.conn().execute(
        "SELECT COUNT(*) FROM signal WHERE kind='owns_production_language'"
    ).fetchone()[0] == 0


def test_stale_competitor_keyword_signals_are_purged():
    """"harness" was replaced with "harness.io" because the bare word matched
    generic phrases like "we harness the power of AI" — the old dedupe_key
    never matches the new one, so the wrong signal just sits there forever
    unless purged explicitly."""
    aid = db.upsert_account("stale-comp.com")
    db.add_signal(aid, "competitor_displacement", "ats", "stale-comp.com:comp_d:harness",
                  detail="Uses harness, a direct competitor")
    real = db.add_signal(aid, "competitor_displacement", "ats", "stale-comp.com:comp_d:incident.io",
                         detail="Uses incident.io, a direct competitor")
    assert real is True

    db.init()

    left = {r["dedupe_key"] for r in db.conn().execute(
        "SELECT dedupe_key FROM signal WHERE account_id=?", (aid,))}
    assert "stale-comp.com:comp_d:harness" not in left
    assert "stale-comp.com:comp_d:incident.io" in left


def test_hosting_signals_from_job_ad_text_are_purged_but_techstack_ones_survive():
    """AWS/Azure/Google Cloud/Vercel etc. used to fire from a job-ad mention
    (source='ats'), which is how a company ended up "hosted on" four clouds
    at once. The jd: fingerprint for these was removed, but already-stored
    signals don't self-correct — db.init() has to sweep them."""
    aid = db.upsert_account("stale-hosting.com")
    db.add_signal(aid, "integration_detected", "ats", "stale-hosting.com:integration:AWS",
                  detail="AWS named in job posts", payload={"tool": "AWS"})
    real = db.add_signal(aid, "integration_detected", "techstack", "stale-hosting.com:integration:Vercel",
                         detail="Vercel detected via headers", payload={"tool": "Vercel"})
    assert real is True

    db.init()

    left = {r["dedupe_key"] for r in db.conn().execute(
        "SELECT dedupe_key FROM signal WHERE account_id=?", (aid,))}
    assert "stale-hosting.com:integration:AWS" not in left
    assert "stale-hosting.com:integration:Vercel" in left


def test_init_disqualifies_accounts_outside_the_tightened_headcount_band():
    """ICP band tightened from 5-150 to match headcount_band's 8-60 — an
    account enriched back when 65 employees was in-band must get disqualified
    retroactively, or it keeps showing up on every list page forever."""
    too_big = db.upsert_account("too-big-headcount.com", headcount=65)
    too_small = db.upsert_account("too-small-headcount.com", headcount=6)
    in_band = db.upsert_account("in-band-headcount.com", headcount=30)
    unknown = db.upsert_account("unknown-headcount.com")

    db.init()

    rows = {r["id"]: r["disqualified"] for r in db.conn().execute(
        "SELECT id, disqualified FROM account WHERE id IN (?,?,?,?)",
        (too_big, too_small, in_band, unknown))}
    assert rows[too_big] is not None
    assert rows[too_small] is not None
    assert rows[in_band] is None
    assert rows[unknown] is None


def test_httpx_request_logging_cannot_leak_credentials():
    """MillionVerifier only accepts its key as a URL query param, and httpx
    logs full request URLs (including query strings) at INFO by default.
    Every verify call was writing the live key to plaintext logs — on Railway
    that lands directly in the log viewer."""
    import logging
    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING
