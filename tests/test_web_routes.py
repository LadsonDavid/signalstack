"""Route-level regression tests for the /investigate audit findings.

Everything else in this suite tests functions directly; these two bugs live
in the HTTP layer itself (a form submit's response type, a truthy-string
fallback), so a real request through the FastAPI app is the only way to
reproduce them at all.
"""
from __future__ import annotations

from starlette.testclient import TestClient

from app import db, llm, scoring
from app.web import app


def _post(client: TestClient, path: str, **data):
    return client.post(path, data=data, follow_redirects=False)


# ---------------------------------------------------------------- attach()

def test_attaching_a_none_labelled_post_still_creates_a_real_signal():
    """Regression: db.add_signal(aid, p["label"] or "stated_intent", ...) never
    fell back, because "none" is a non-empty (truthy) string in Python. 291 of
    384 real posts were labelled "none" — attaching any of them linked the
    post and marked it reviewed, then created kind="none", which scoring
    silently ignores because it isn't in malveon.yaml's intent: block."""
    with db.tx() as c:
        c.execute("INSERT INTO intent_post(source,ext_id,title,body,label,created_at) "
                  "VALUES(?,?,?,?,?,?)",
                  ("hn", "post1", "Rust 1.94 released", "unrelated release notes",
                   "none", db.now()))
    post_id = db.conn().execute("SELECT id FROM intent_post WHERE ext_id='post1'").fetchone()["id"]

    with TestClient(app) as client:
        r = _post(client, f"/feed/{post_id}/attach", domain="attach-test.com")
    assert r.status_code == 303

    aid = db.conn().execute("SELECT id FROM account WHERE domain='attach-test.com'").fetchone()["id"]
    rows = db.signals_for(aid)
    assert len(rows) == 1
    assert rows[0]["kind"] != "none"
    assert rows[0]["kind"] in llm.PROMOTABLE_LABELS
    # And it must actually count — the whole point of the bug being invisible
    # was that the created signal contributed nothing to score.
    assert scoring.score_rows(rows)["intent"] > 0


def test_attaching_a_classified_post_preserves_the_real_label():
    """A post the classifier already scored competitor_gripe must keep that
    label on manual attach, not get forced to stated_intent."""
    with db.tx() as c:
        c.execute("INSERT INTO intent_post(source,ext_id,title,body,label,created_at) "
                  "VALUES(?,?,?,?,?,?)",
                  ("hn", "post2", "Datadog bill tripled", "looking for alternatives",
                   "competitor_gripe", db.now()))
    post_id = db.conn().execute("SELECT id FROM intent_post WHERE ext_id='post2'").fetchone()["id"]

    with TestClient(app) as client:
        _post(client, f"/feed/{post_id}/attach", domain="preserve-label.com")

    aid = db.conn().execute("SELECT id FROM account WHERE domain='preserve-label.com'").fetchone()["id"]
    assert db.signals_for(aid)[0]["kind"] == "competitor_gripe"


# ---------------------------------------------------------------- feed

def test_feed_hides_posts_the_classifier_scored_not_relevant():
    """Posts labelled "none" aren't about a Malveon problem at all — the user
    asked for these to stop cluttering the feed instead of sitting behind a
    "Not relevant" toggle nobody needs to click."""
    with db.tx() as c:
        c.execute("INSERT INTO intent_post(source,ext_id,title,body,label,created_at) "
                  "VALUES(?,?,?,?,?,?)",
                  ("hn", "irrelevant1", "Rust 1.94 released", "unrelated release notes",
                   "none", db.now()))
        c.execute("INSERT INTO intent_post(source,ext_id,title,body,label,created_at) "
                  "VALUES(?,?,?,?,?,?)",
                  ("hn", "relevant1", "Our incidents are out of control", "looking for tools",
                   "active_research", db.now()))

    with TestClient(app) as client:
        r = client.get("/feed")
    assert "Rust 1.94 released" not in r.text
    assert "Our incidents are out of control" in r.text
    assert "Not relevant" not in r.text


def test_feed_label_filter_still_applies_after_excluding_none():
    """Regression: 'WHERE label != none OR label IS NULL' + 'AND label=?'
    parses as 'WHERE label != none OR (label IS NULL AND label=?)' — SQL's
    AND binds tighter than OR, so the none-exclusion swallowed the label
    filter and every tab showed the same unfiltered list."""
    with db.tx() as c:
        c.execute("INSERT INTO intent_post(source,ext_id,title,body,label,created_at) "
                  "VALUES(?,?,?,?,?,?)",
                  ("hn", "looking1", "Evaluating incident tools", "just browsing options",
                   "active_research", db.now()))
        c.execute("INSERT INTO intent_post(source,ext_id,title,body,label,created_at) "
                  "VALUES(?,?,?,?,?,?)",
                  ("hn", "ready1", "Ready to switch off PagerDuty", "budget approved",
                   "stated_intent", db.now()))

    with TestClient(app) as client:
        r = client.get("/feed?label=stated_intent")
    assert "Ready to switch off PagerDuty" in r.text
    assert "Evaluating incident tools" not in r.text


# ---------------------------------------------------------------- classify

def test_pixel_accepts_post_because_sendbeacon_always_posts():
    """Regression: /px was registered GET-only, so every real visit was rejected
    405. navigator.sendBeacon ALWAYS sends POST (per spec), and it returns true
    on queueing regardless of the response — so the failure was invisible in the
    browser and the visits page just stayed empty forever."""
    with TestClient(app) as client:
        post = client.post("/px?p=/pricing&r=https://google.com&s=22",
                           headers={"user-agent": "Mozilla/5.0 (Macintosh) Safari/605",
                                    "x-forwarded-for": "203.0.113.50"})
        get = client.get("/px?p=/pricing&s=22",
                         headers={"user-agent": "Mozilla/5.0 (Macintosh) Safari/605",
                                  "x-forwarded-for": "203.0.113.51"})

    assert post.status_code == 200, "sendBeacon POSTs; rejecting it drops every visit"
    assert get.status_code == 200, "<img> fallback still needs GET"
    # Cross-origin from malveon.com, so the browser requires this header.
    assert post.headers.get("access-control-allow-origin") == "*"


def test_cloudflare_client_ip_wins_over_the_proxy_chain():
    """Behind px.malveon.com there are two proxies (Cloudflare, then Railway),
    so X-Forwarded-For reads "visitor, cloudflare, railway". Trusting the wrong
    entry doesn't error — it silently resolves every visit to a datacenter
    instead of the visitor's company."""
    from app.web import _client_ip

    class Req:
        def __init__(self, headers):
            self.headers = headers
            self.client = None

    # Cloudflare present: its header is authoritative.
    assert _client_ip(Req({"cf-connecting-ip": "203.0.113.9",
                           "x-forwarded-for": "203.0.113.9, 172.70.1.1"})) == "203.0.113.9"
    # No Cloudflare: first entry of the chain is the visitor.
    assert _client_ip(Req({"x-forwarded-for": "198.51.100.4, 10.0.0.1"})) == "198.51.100.4"
    # Neither header present.
    assert _client_ip(Req({})) == ""


def test_pixel_answers_cors_preflight():
    with TestClient(app) as client:
        r = client.options("/px")
    assert r.status_code == 204
    assert "POST" in r.headers.get("access-control-allow-methods", "")


def test_classify_pending_redirects_instead_of_stranding_on_raw_json(monkeypatch):
    """Regression: the button is a plain <form method="post"> submit, not a
    fetch() call. Returning JSONResponse made the browser navigate away from
    the styled app to a bare JSON page with no nav bar and no way back —
    every other action route in web.py redirects; this one didn't.

    classify_pending is stubbed out: this test is only about the route's
    response type, and the real function would otherwise make live LLM calls
    on every test run.
    """
    import app.web as web

    monkeypatch.setattr(web.llm, "classify_pending",
                        lambda **kw: {"classified": 0, "promoted": 0, "pending": 0})

    with TestClient(app) as client:
        r = _post(client, "/ops/classify")

    assert r.status_code == 303
    assert r.headers["location"] == "/ops"


# ---------------------------------------------------------------- healthz

def test_healthz_reports_unhealthy_when_the_scheduler_has_gone_quiet():
    """Regression: /healthz always said ok:true just because the web process
    was up, even if every collector had died. An external uptime check needs
    to see the same thing you'd see on /ops — is this actually working."""
    with db.tx() as c:
        c.execute("DELETE FROM collector_run")
        c.execute("INSERT INTO collector_run(collector,started_at,status) VALUES(?,?,?)",
                  ("hn", "2020-01-01T00:00:00+00:00", "ok"))

    with TestClient(app) as client:
        r = client.get("/healthz")
    body = r.json()
    assert body["ok"] is False
    assert body["scheduler_alive"] is False


def test_healthz_is_healthy_right_after_boot_before_anything_has_run():
    with db.tx() as c:
        c.execute("DELETE FROM collector_run")

    with TestClient(app) as client:
        r = client.get("/healthz")
    assert r.json()["ok"] is True


# ---------------------------------------------------------------- backup

def test_backup_downloads_a_valid_sqlite_file():
    db.upsert_account("backup-check.com")
    with TestClient(app) as client:
        r = client.get("/ops/backup")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/octet-stream"
    assert r.content[:16] == b"SQLite format 3\x00"


# ---------------------------------------------------------------- pain points

def test_pain_points_page_renders():
    db.upsert_account("pain-route-check.com")
    aid = db.conn().execute(
        "SELECT id FROM account WHERE domain='pain-route-check.com'").fetchone()["id"]
    db.add_signal(aid, "active_research", "hn", "pr1", detail="Reading up on incident tooling")

    with TestClient(app) as client:
        r = client.get("/pain-points")
    assert r.status_code == 200
    assert "pain-route-check.com" in r.text
