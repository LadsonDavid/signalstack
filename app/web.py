"""FastAPI + server-rendered Jinja. No build step, no bundler.

The page has exactly one job: which engineering teams do I contact this week,
and what do I say. The ranked table with visible reasons IS the product; the
three lead types are filter chips over one table, not three pages.
"""
from __future__ import annotations

import contextlib
import hmac
import logging
import os
import pathlib
import sqlite3
import tempfile
from datetime import datetime, timezone

from fastapi import FastAPI, Form, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.background import BackgroundTask

from . import contacts, db, labels, leads, llm, tracking
from .config import DATA_DIR, Env, cfg, reload_cfg

log = logging.getLogger("web")


@contextlib.asynccontextmanager
async def lifespan(_: FastAPI):
    db.init()
    yield


app = FastAPI(title="Malveon Lead Engine", lifespan=lifespan)
templates = Jinja2Templates(directory=str(pathlib.Path(__file__).parent / "templates"))

# Exposed as globals rather than passed per-route: every page renders at least
# one of these, and threading them through each handler is how the same status
# ended up rendering three different ways across templates.
templates.env.globals.update(
    email_status=labels.email_status,
    post_label=labels.post_label,
    lead_type=labels.lead_type,
    source_name=labels.source,
    signal_kind=labels.signal_kind,
    FIT_HELP=labels.FIT_HELP,
    INTENT_HELP=labels.INTENT_HELP,
    SCORE_HELP=labels.SCORE_HELP,
)


@app.middleware("http")
async def gate(request: Request, call_next):
    """Optional shared-secret gate for the hosted deployment."""
    # The pixel endpoints must stay public — they are called by malveon.com.
    if Env.UI_KEY and request.url.path not in ("/healthz", "/px", "/px.js"):
        # The x-key header is for programs (malves): it keeps the key out of URLs.
        given = (request.query_params.get("key"), request.cookies.get("k"),
                 request.headers.get("x-key"))
        if not any(g and hmac.compare_digest(g, Env.UI_KEY) for g in given):
            return HTMLResponse("<h1>401</h1><p>append ?key=…</p>", status_code=401)
    resp = await call_next(request)
    if Env.UI_KEY and request.query_params.get("key") == Env.UI_KEY:
        resp.set_cookie("k", Env.UI_KEY, httponly=True, max_age=60 * 60 * 24 * 30)
    return resp


_BOOT = datetime.now(timezone.utc)


@app.get("/healthz")
def healthz():
    """`ok` used to mean "the web process is up," which is not the same thing
    as "this tool is actually working" — every collector could die silently
    and this would still say true forever. Now it also asks: has ANYTHING
    actually run recently? That's what an external uptime check should watch,
    not just whether FastAPI answers a request.
    """
    row = db.conn().execute("SELECT MAX(started_at) m FROM collector_run").fetchone()
    last_run = row["m"] if row else None
    if last_run:
        age_min = (datetime.now(timezone.utc)
                   - datetime.fromisoformat(last_run.replace("Z", "+00:00"))).total_seconds() / 60
        scheduler_alive = age_min < 60
    else:
        # Nothing has run yet — fine for the first few minutes after a fresh
        # boot/deploy (the scheduler waits one interval before its first
        # fire), a real problem past that.
        scheduler_alive = (datetime.now(timezone.utc) - _BOOT).total_seconds() / 60 < 45
    return {"ok": scheduler_alive, "scheduler_alive": scheduler_alive,
            "last_collector_run": last_run, **leads.stats()}


@app.get("/", response_class=HTMLResponse)
def board(request: Request, type: str | None = Query(None), watch: int = 0,
          q: str | None = None):
    rows = leads.board(lead_type=type, watchlist=bool(watch), q=q)
    return templates.TemplateResponse(request, "board.html", {
        "rows": rows, "type": type, "watch": watch, "q": q or "",
        "stats": leads.stats(), "thresholds": cfg()["thresholds"],
    })


@app.get("/api/leads")
def api_leads(type: str | None = Query(None), limit: int = Query(25, ge=1, le=100)):
    """The board as JSON, for malves' phone app: who to contact this week, and why.

    Fields are picked one by one rather than dumping the board row: the row
    carries sets and internal scoring detail that aren't JSON and aren't for a
    phone screen.
    """
    return {
        "generated_at": db.now(),
        "leads": [_lead_json(r) for r in leads.board(lead_type=type, limit=limit)],
    }


def _lead_json(r: dict) -> dict:
    # board() already sorted contacts best-first and dropped known-bad addresses.
    best = (r.get("contacts") or [None])[0]
    return {
        "domain": r["domain"],
        "name": r.get("name") or r["domain"],
        "tier": r["tier"],
        "score": r["score"],
        "fit": r["fit"],
        "intent": r["intent"],
        "types": r["types"],
        "why": r["why"],
        "trigger": r.get("trigger") or "",
        "opener": r.get("best_line_hook") or "",
        "contact": {k: best.get(k) or "" for k in ("name", "title", "email", "status")}
        if best else None,
        "signals": r["n_signals"],
        "last_signal": r.get("last_signal") or "",
    }


@app.get("/pain-points", response_class=HTMLResponse)
def pain_points(request: Request):
    return templates.TemplateResponse(request, "pain_points.html", {
        "rows": leads.pain_points(),
    })


@app.get("/a/{domain}", response_class=HTMLResponse)
def account(request: Request, domain: str):
    d = leads.detail(domain)
    if not d:
        raise HTTPException(404, "unknown account")
    return templates.TemplateResponse(request, "detail.html", {
        "a": d, "product": cfg()["product"],
        # The tagged link they paste into outreach. Certain attribution, unlike
        # reverse-IP which cannot name a company this size at all.
        "track_link": f"https://{cfg()['product']['domain']}/?via={domain}",
        "product_lines": cfg()["product_lines"], "personas": cfg()["personas"]})


@app.post("/a/{domain}/watch")
def watch(domain: str):
    with db.tx() as c:
        c.execute("UPDATE account SET watchlisted = 1 - watchlisted WHERE domain=?", (domain,))
    return RedirectResponse(f"/a/{domain}", status_code=303)


@app.post("/a/{domain}/disqualify")
def dq(domain: str, reason: str = Form("manual")):
    row = db.conn().execute("SELECT id,disqualified FROM account WHERE domain=?", (domain,)).fetchone()
    if row:
        db.disqualify(row["id"], None if row["disqualified"] else reason)
    return RedirectResponse(f"/a/{domain}", status_code=303)


@app.post("/a/{domain}/enrich")
def enrich(domain: str):
    contacts.enrich_account(domain)
    return RedirectResponse(f"/a/{domain}", status_code=303)


@app.post("/a/{domain}/verify")
def verify_one(domain: str, email: str = Form(...)):
    res = contacts.verify(email)
    row = db.conn().execute("SELECT id FROM account WHERE domain=?", (domain,)).fetchone()
    if row:
        with db.tx() as c:
            c.execute("INSERT INTO contact(account_id,email,status,source,created_at) "
                      "VALUES(?,?,?,?,?) ON CONFLICT(account_id,email) "
                      "DO UPDATE SET status=excluded.status",
                      (row["id"], email.lower().strip(), res["status"], "manual", db.now()))
    return RedirectResponse(f"/a/{domain}", status_code=303)


@app.post("/add")
def add_account(domain: str = Form(...)):
    d = db.norm_domain(domain)
    if d:
        db.upsert_account(d)
        return RedirectResponse(f"/a/{d}", status_code=303)
    return RedirectResponse("/", status_code=303)


@app.get("/feed", response_class=HTMLResponse)
def feed(request: Request, label: str | None = None):
    # Posts the classifier scored "none" aren't about a Malveon problem at
    # all — showing them by default just buries the posts worth reading.
    sql = "SELECT p.*, a.domain FROM intent_post p LEFT JOIN account a ON a.id=p.account_id WHERE (p.label != 'none' OR p.label IS NULL)"
    params: tuple = ()
    if label:
        sql += " AND p.label=?"
        params = (label,)
    sql += " ORDER BY p.created_at DESC LIMIT 200"
    posts = [dict(r) for r in db.conn().execute(sql, params)]
    return templates.TemplateResponse(request, "feed.html", {
        "posts": posts, "label": label})


@app.post("/feed/{post_id}/attach")
def attach(post_id: int, domain: str = Form(...)):
    """Manual attribution — most public posts cannot be auto-attributed, and
    guessing would manufacture fake precision.

    `p["label"]` is a non-empty string like "none" as often as it's NULL, and
    "none" is truthy in Python — `p["label"] or "stated_intent"` only ever
    fell back on the NULL case. A human clicking attach on a post the
    classifier scored "none" is overriding that verdict, so the signal must
    still land on a kind the scorer actually knows about, not silently vanish
    as kind="none".
    """
    aid = db.upsert_account(domain)
    p = db.conn().execute("SELECT * FROM intent_post WHERE id=?", (post_id,)).fetchone()
    if aid and p:
        with db.tx() as c:
            c.execute("UPDATE intent_post SET account_id=?,reviewed=1 WHERE id=?", (aid, post_id))
        label = p["label"] if p["label"] in llm.PROMOTABLE_LABELS else "stated_intent"
        db.add_signal(aid, label, "manual",
                      f"post:{p['source']}:{p['ext_id']}:manual",
                      detail=(p["title"] or p["body"] or "")[:140],
                      url=p["url"] or "", observed_at=p["created_at"])
    return RedirectResponse("/feed", status_code=303)


# ---------------------------------------------------------------- site pixel

# malveon.com calls this cross-origin, so the browser needs these on both the
# preflight and the real response.
_PX_CORS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Max-Age": "86400",
}


def _client_ip(request: Request) -> str:
    """The visitor's IP, not a proxy's.

    Order matters. Once px.malveon.com sits behind Cloudflare there are two
    proxies in front of this app, so X-Forwarded-For becomes a chain like
    "visitor, cloudflare, railway". CF-Connecting-IP is set by Cloudflare to
    the true client and cannot be spoofed through their edge, so it wins when
    present. Getting this wrong doesn't error — it silently resolves every
    visit to Cloudflare's datacenter instead of the visitor's company.
    """
    cf = (request.headers.get("cf-connecting-ip") or "").strip()
    if cf:
        return cf
    fwd = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    if fwd:
        return fwd
    return request.client.host if request.client else ""


@app.get("/px.js")
def pixel_js(request: Request):
    """Embed on malveon.com: <script async src="https://THIS-HOST/px.js"></script>"""
    base = str(request.base_url).rstrip("/")
    return Response(tracking.PIXEL_JS % {"base": base}, media_type="application/javascript",
                    headers={"Cache-Control": "public, max-age=3600"})


@app.api_route("/px", methods=["GET", "POST", "OPTIONS"])
def pixel(request: Request, p: str = "/", r: str = "", s: float | None = None,
          via: str = ""):
    """Records one visit.

    Must accept POST: navigator.sendBeacon ALWAYS sends POST — that is in the
    spec, not a browser quirk — and the pixel prefers sendBeacon because it
    survives the page being closed. Registered as GET-only, every real visit was
    rejected with 405 while sendBeacon still returned true (it only reports that
    the request was queued, never what the server answered), so the failure was
    completely silent from the browser's side.

    GET stays supported for the <img> fallback used when sendBeacon is missing.
    """
    if request.method == "OPTIONS":                    # CORS preflight
        return Response(status_code=204, headers=_PX_CORS)

    ip = _client_ip(request)
    tracking.record_visit(ip=ip, path=p, ua=request.headers.get("user-agent", ""),
                          referrer=r, seconds=s, via=via)
    # 1x1 transparent GIF so the <img> fallback renders cleanly.
    return Response(
        b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\x00\x00\x00!\xf9\x04\x01"
        b"\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;",
        media_type="image/gif",
        headers={"Cache-Control": "no-store", **_PX_CORS})


@app.get("/visits", response_class=HTMLResponse)
def visits(request: Request):
    c = db.conn()
    rows = [dict(x) for x in c.execute(
        "SELECT v.*, a.domain FROM site_visit v LEFT JOIN account a ON a.id=v.account_id "
        "ORDER BY v.created_at DESC LIMIT 250")]
    by_res = {x["resolution"]: x["n"] for x in c.execute(
        "SELECT resolution, COUNT(*) n FROM site_visit GROUP BY resolution")}
    return templates.TemplateResponse(request, "visits.html", {
        "rows": rows, "by_res": by_res,
        "host": str(request.base_url).rstrip("/"),
        "identified": sum(v for k, v in by_res.items() if k not in ("bot", "none", "isp")),
        "total": sum(by_res.values()),
    })


@app.get("/ops", response_class=HTMLResponse)
def ops(request: Request, ran: str = "", status: str = "", records: int = 0, signals: int = 0):
    c = db.conn()
    # Presence only, never values. A key that silently failed to reach the
    # deployment looks identical to a provider being "not configured", which
    # is how five keys sat unread in a local .env for a day.
    keys = {
        "GitHub": bool(Env.GITHUB_TOKEN), "PDL": bool(Env.PDL_KEY),
        "LLM": bool(Env.LLM_API_KEY), "ipinfo": bool(Env.IPINFO_TOKEN),
        "MillionVerifier": bool(Env.MILLIONVERIFIER_KEY),
        "Reddit": bool(Env.REDDIT_ID and Env.REDDIT_SECRET),
        "Product Hunt": bool(Env.PRODUCTHUNT_TOKEN),
    }
    runs = [dict(r) for r in c.execute(
        "SELECT * FROM collector_run ORDER BY started_at DESC LIMIT 60")]
    breakers = [dict(r) for r in c.execute("SELECT * FROM breaker")]

    # One plain-English verdict, so the page answers "is it working?" without
    # the user having to interpret a failure table.
    paused = [b for b in breakers if b.get("opened_until")]
    failed = [r for r in runs[:15] if r.get("status") == "failed"]
    if paused:
        health = {"ok": False,
                  "detail": f"{len(paused)} source(s) paused after repeated errors. "
                            "They'll retry automatically."}
    elif failed:
        health = {"ok": False,
                  "detail": f"{len(failed)} recent check(s) failed. See technical details below."}
    else:
        last = runs[0]["started_at"][:16].replace("T", " ") if runs else "not yet"
        health = {"ok": True, "detail": f"Last checked {last}."}

    # Result of a "check now" button click, if that's how we got here — shown
    # right at the top so clicking a button visibly does something instead of
    # just reloading the page with no sign of what happened.
    just_ran = None
    if ran:
        label = labels.source(ran)
        if status == "ok":
            if signals:
                just_ran = {"ok": True, "msg":
                            f"Checked {label} — found {signals} new thing"
                            + ("s" if signals != 1 else "") + "."}
            elif records:
                just_ran = {"ok": True, "msg": f"Checked {label} — nothing new this time."}
            else:
                just_ran = {"ok": True, "msg": f"Checked {label} — nothing to check right now."}
        elif status == "skipped_open_breaker":
            just_ran = {"ok": False, "msg":
                        f"{label} is paused after repeated errors — it'll retry on its own later."}
        elif status == "failed":
            just_ran = {"ok": False, "msg": f"{label} check failed — it'll retry automatically."}
        else:
            just_ran = {"ok": True, "msg": f"Checked {label}."}

    return templates.TemplateResponse(request, "ops.html", {
        "runs": runs,
        "breakers": breakers,
        "stats": leads.stats(),
        "keys": keys,
        "health": health,
        "sources": labels.source_buttons(),
        "data_dir": str(DATA_DIR),
        "db_rows": c.execute("SELECT COUNT(*) FROM signal").fetchone()[0],
        "pending": c.execute("SELECT COUNT(*) FROM intent_post WHERE label IS NULL").fetchone()[0],
        "just_ran": just_ran,
    })


@app.post("/ops/run")
def ops_run(collector: str = Form(...)):
    from .scheduler import run_one
    res = run_one(collector)
    return RedirectResponse(
        f"/ops?ran={collector}&status={res.get('status', '')}"
        f"&records={res.get('records', 0)}&signals={res.get('signals', 0)}",
        status_code=303)


@app.post("/ops/reload")
def ops_reload():
    reload_cfg()
    return RedirectResponse("/ops", status_code=303)


@app.get("/ops/backup")
def ops_backup():
    """Everything lives in one SQLite file on one Railway volume — no backup.
    sqlite3's own .backup() is used rather than copying the file directly,
    which is safe under WAL even while the app keeps writing to it, unlike a
    raw file copy that can capture a half-written state.
    """
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    dest = sqlite3.connect(path)
    with dest:
        db.conn().backup(dest)
    dest.close()
    name = f"malveon-leads-{db.now()[:10]}.db"
    return FileResponse(path, filename=name, media_type="application/octet-stream",
                        background=BackgroundTask(os.remove, path))


@app.post("/ops/classify")
def ops_classify():
    # This is a plain HTML <form method="post"> submit, not a fetch() call —
    # returning JSONResponse made the browser navigate away from the app to a
    # bare unstyled JSON page with no nav bar and no way back. Every other
    # action route in this file redirects back to its page; this one didn't.
    llm.classify_pending()
    return RedirectResponse("/ops", status_code=303)
