"""The board as JSON (`GET /api/leads`), for malves' phone app.

The phone shows the same thing the board does — who to contact this week, and
why — so these check that the JSON keeps the board's promises: same order, no
disqualified accounts, never a known-bad address as the contact, and nothing
that isn't plain JSON.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from starlette.testclient import TestClient

from app import db
from app.config import Env
from app.web import app


def _ago(days: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")


def _contact(aid: int, name: str, title: str, email: str, status: str) -> None:
    with db.tx() as c:
        c.execute(
            "INSERT INTO contact(account_id,name,title,email,status,source,created_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (aid, name, title, email, status, "test", db.now()))


def _seed() -> None:
    hot = db.upsert_account("hot.example", name="Hot Co", headcount=40)
    db.add_signal(hot, "site_visit_pricing", "pixel", "hot-1", detail="Read the pricing page",
                  observed_at=_ago(1))
    db.add_signal(hot, "stated_intent", "hn", "hot-2", detail="Asked for a tool like this",
                  observed_at=_ago(2))
    _contact(hot, "Bad Address", "VP Engineering", "bad@hot.example", "invalid")
    _contact(hot, "Ada Lovelace", "VP Engineering", "ada@hot.example", "valid")

    warm = db.upsert_account("warm.example", name="Warm Co", headcount=40)
    db.add_signal(warm, "site_visit", "pixel", "warm-1", detail="Read the homepage",
                  observed_at=_ago(3))

    gone = db.upsert_account("gone.example", name="Gone Co")
    db.add_signal(gone, "site_visit_pricing", "pixel", "gone-1", observed_at=_ago(1))
    db.disqualify(gone, "not a fit")


def test_api_leads_mirrors_the_board_as_plain_json():
    _seed()
    with TestClient(app) as client:
        r = client.get("/api/leads")
    assert r.status_code == 200
    body = r.json()
    domains = [lead["domain"] for lead in body["leads"]]

    assert domains[0] == "hot.example"          # highest score first, like the board
    assert "gone.example" not in domains         # disqualified never shows up
    top = body["leads"][0]
    assert set(top) == {"domain", "name", "tier", "score", "fit", "intent", "types",
                        "why", "trigger", "opener", "contact", "signals", "last_signal"}
    assert top["name"] == "Hot Co"
    assert top["tier"] in ("hot", "warm", "cold")
    assert top["why"]                            # every lead says why it's here
    assert top["signals"] == 2
    # A known-bad address must never be the one to email.
    assert top["contact"] == {"name": "Ada Lovelace", "title": "VP Engineering",
                              "email": "ada@hot.example", "status": "valid"}
    assert body["generated_at"]


def test_api_leads_limit_and_no_contact():
    _seed()
    with TestClient(app) as client:
        body = client.get("/api/leads?limit=1").json()
        assert len(body["leads"]) == 1
        warm = next(x for x in client.get("/api/leads").json()["leads"]
                    if x["domain"] == "warm.example")
    assert warm["contact"] is None


def test_api_leads_respects_the_ui_key_and_accepts_it_as_a_header(monkeypatch):
    _seed()
    monkeypatch.setattr(Env, "UI_KEY", "s3cret")
    with TestClient(app) as client:
        assert client.get("/api/leads").status_code == 401
        assert client.get("/api/leads", headers={"x-key": "wrong"}).status_code == 401
        assert client.get("/api/leads", headers={"x-key": "s3cret"}).status_code == 200
