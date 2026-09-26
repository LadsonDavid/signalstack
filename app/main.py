"""Single-process entrypoint: scheduler + web UI.

One service, not three. Railway volumes attach to a single service anyway, and
a separate worker plus a broker would cost more per month than the data budget.
"""
from __future__ import annotations

import logging
import os
import pathlib

import uvicorn

from . import db
from .web import app

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)-5s %(name)-10s %(message)s")
log = logging.getLogger("main")


def _seed_if_empty() -> None:
    """A fresh volume starts with an empty database, and the first scheduled
    collector run is a full interval away — so a new deploy would sit doing
    nothing for a day. Seeding on first boot makes a deploy self-populating.
    Idempotent: upsert_account means re-running changes nothing.
    """
    if db.conn().execute("SELECT COUNT(*) FROM account").fetchone()[0]:
        return
    path = pathlib.Path(__file__).resolve().parent.parent / "seeds.txt"
    if not path.exists():
        return
    n = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#")[0].strip()
        if not line:
            continue
        parts = line.split(":")
        ok = (db.upsert_account(parts[2], ats=parts[0], ats_token=parts[1])
              if len(parts) == 3 else db.upsert_account(parts[0]))
        n += bool(ok)
    log.info("first boot: seeded %d accounts", n)


def serve(port: int | None = None) -> None:
    db.init()
    _seed_if_empty()
    if os.getenv("DISABLE_SCHEDULER") != "1":
        from .scheduler import start
        start()
        log.info("scheduler running")
    uvicorn.run(app, host="0.0.0.0", port=port or int(os.getenv("PORT", 8000)),
                log_level="info")


if __name__ == "__main__":
    serve()
