"""CLI. `python -m app.cli <cmd>`"""
from __future__ import annotations

import json
import logging
import sys

# Company names and incident titles routinely contain characters the Windows
# console's cp1252 default cannot encode, which crashed the board printout.
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-5s %(name)-10s %(message)s")


def _seed(path: str) -> None:
    """seeds.txt lines: `domain.com` or `platform:token:domain.com`."""
    from . import db
    db.init()
    n = 0
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.split("#")[0].strip()
            if not line:
                continue
            parts = line.split(":")
            if len(parts) == 3:
                plat, tok, dom = parts
                ok = db.upsert_account(dom, ats=plat.strip(), ats_token=tok.strip())
            else:
                ok = db.upsert_account(parts[0])
            n += bool(ok)
    print(f"seeded {n} accounts")


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__ + """
  init                       create the database
  seed <file>                load domains / boards from a seeds file
  discover <domain>          find which ATS a company uses
  run <collector>            statuspage | stargazers | hn_hiring | yc | vendors | jobfeeds
                             ats | techstack | mastodon | devto | edgar | hn | github | lobsters
  run-all                    every enabled collector, in order
  smoke <collector>          hit the real endpoint once and print what parsed
  score [domain]             print scores (all, or one account in detail)
  enrich <domain>            headcount + decision-maker contacts
  verify <email>             run the email waterfall
  classify                   label pending intent posts with the LLM
  eval                       precision/recall on eval/intent_labels.json
  serve [port]               scheduler + web UI""")
        return 1

    cmd, args = argv[0], argv[1:]
    from . import db

    if cmd == "init":
        db.init()
        print("ok")

    elif cmd == "seed":
        _seed(args[0])

    elif cmd == "discover":
        from .collectors.ats import discover
        print(discover(args[0]) or "no public board found")

    elif cmd == "run":
        from .scheduler import run_one
        print(json.dumps(run_one(args[0]), indent=2))

    elif cmd == "run-all":
        from .config import cfg
        from .scheduler import run_one
        for name, spec in cfg()["collectors"].items():
            if spec.get("enabled"):
                print(json.dumps(run_one(name), indent=2))

    elif cmd == "smoke":
        db.init()
        name = args[0]
        if name == "ats":
            from .collectors.ats import ADAPTERS
            for plat, fn in ADAPTERS.items():
                tok = {"greenhouse": "stripe", "lever": "spotify", "ashby": "linear",
                       "workable": "hotjar", "smartrecruiters": "Twilio"}[plat]
                try:
                    jobs = fn(tok)
                    print(f"{plat:16} {tok:10} {len(jobs):4} jobs  "
                          f"first={jobs[0]['title'][:50] if jobs else '-'}")
                except Exception as exc:
                    print(f"{plat:16} {tok:10} FAILED {exc}")
        elif name == "techstack":
            from .collectors.techstack import detect, probe
            for d in (args[1:] or ["linear.app", "supabase.com"]):
                ev = probe(d)
                print(f"{d}: status_page={ev['status_page']} tools={detect(ev)}")
        elif name == "edgar":
            from .collectors.edgar import _filings
            f = _filings(3)
            print(f"{len(f)} Form D filings in last 3 business days")
            for x in f[:5]:
                print(" ", x["company"], x["date"])
        else:
            from .scheduler import run_one
            print(json.dumps(run_one(name), indent=2))

    elif cmd == "reset-source":
        # Retuning a matcher invalidates everything it previously emitted. The
        # signal log is the source of truth but a source's own slice of it is
        # derived from that source's rules, so it must be rebuildable.
        db.init()
        src = args[0]
        with db.tx() as c:
            n = c.execute("DELETE FROM signal WHERE source=? OR source LIKE ?",
                          (src, f"llm:{src}")).rowcount
            p = c.execute("DELETE FROM intent_post WHERE source=?", (src,)).rowcount
            s = c.execute("DELETE FROM snapshot WHERE collector=?", (src,)).rowcount
        print(f"purged {n} signals, {p} posts, {s} snapshots for {src!r} — re-run to rebuild")

    elif cmd == "prune":
        # Discovery pulls in ATS shorteners, job boards and recruiting tools.
        # They are not prospects; disqualify rather than delete so they don't
        # get re-discovered and silently reappear next sweep.
        from .collectors.discovery import NOT_A_COMPANY
        db.init()
        n = 0
        for r in db.conn().execute(
                "SELECT id,domain FROM account WHERE disqualified IS NULL").fetchall():
            if NOT_A_COMPANY.search(r["domain"]):
                db.disqualify(r["id"], "aggregator / recruiting infrastructure")
                n += 1
        print(f"disqualified {n} non-company domains")

    elif cmd == "score":
        from . import leads
        if args:
            d = leads.detail(args[0])
            if not d:
                print("unknown account")
                return 1
            print(f"{d['domain']}  score={d['score']} (fit {d['fit']} × intent {d['intent']})  "
                  f"types={d['types']}")
            for r in d["reasons"]:
                print(f"  +{r['points']:>5}  [{r['axis']}] {r['detail']}")
        else:
            for r in leads.board(limit=50):
                print(f"{r['score']:>3}  fit={r['fit']:>3} int={r['intent']:>3}  "
                      f"{r['domain']:<34} {r['why'][:80]}")

    elif cmd == "enrich":
        from . import contacts
        db.init()
        print(json.dumps(contacts.enrich_account(args[0]), indent=2))

    elif cmd == "enrich-top":
        # PDL's free tier is 100 lookups/month, so spend them on the accounts
        # most likely to matter: highest fit, headcount still unknown.
        from . import contacts, leads
        db.init()
        n = int(args[0]) if args else 25
        todo = [r for r in leads.board(limit=5000) if not r.get("headcount")][:n]
        print(f"enriching {len(todo)} accounts (highest fit first)")
        for r in todo:
            res = contacts.enrich_account(r["domain"], verify_paid=False)
            hc = res.get("headcount")
            row = db.conn().execute("SELECT disqualified FROM account WHERE domain=?",
                                    (r["domain"],)).fetchone()
            flag = f"  DISQUALIFIED: {row['disqualified']}" if row and row["disqualified"] else ""
            print(f"  {r['domain']:26} headcount={hc if hc is not None else '?':>7}"
                  f"  contacts={res.get('total', 0)}{flag}")

    elif cmd == "github":
        from . import contacts
        db.init()
        print(json.dumps(contacts.github_contacts(args[0]), indent=2))
        for r in db.conn().execute(
                "SELECT c.name,c.title,c.email,c.status,c.source FROM contact c "
                "JOIN account a ON a.id=c.account_id WHERE a.domain=? ORDER BY c.status DESC",
                (args[0],)):
            print(f"  {r['status']:18} {r['email']:38} {r['name'][:24]:24} {(r['title'] or '')[:40]}")

    elif cmd == "verify":
        from . import contacts
        print(json.dumps(contacts.verify(args[0]), indent=2))

    elif cmd == "classify":
        from . import llm
        print(json.dumps(llm.classify_pending(), indent=2))

    elif cmd == "eval":
        from . import llm
        print(json.dumps(llm.evaluate(), indent=2))

    elif cmd == "serve":
        from .main import serve
        serve(int(args[0]) if args else 8000)

    else:
        print(f"unknown command {cmd!r}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
