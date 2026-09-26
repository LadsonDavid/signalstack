import os
import pathlib
import tempfile

# Must be set before app.config is imported anywhere.
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="leadtest-")
os.environ.setdefault("CONFIG_PATH",
                      str(pathlib.Path(__file__).resolve().parent.parent / "malveon.yaml"))

import pytest  # noqa: E402

from app import db  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_db():
    # init() only reconciles orphaned collector_run rows once per process by
    # design (see db.py) — reset that between tests so each one still gets
    # to exercise it, same as a real fresh boot would.
    db._reconciled = False
    db.init()
    with db.tx() as c:
        for t in ("signal", "account", "snapshot", "collector_run", "breaker",
                  "contact", "email_verification", "intent_post", "llm_cache",
                  "site_visit", "ip_cache"):
            c.execute(f"DELETE FROM {t}")
    yield db
