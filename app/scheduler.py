"""In-process scheduler. APScheduler, not Celery — a separate worker service and
a Redis broker would cost more per month than the entire data budget.
"""
from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler

from . import db, llm
from .collectors.ats import AtsCollector
from .collectors.base import run_collector
from .collectors.community import (CommunityCollector, ContactsCollector,
                                   StargazerCollector)
from .collectors.discovery import (HnHiringCollector, JobFeedCollector,
                                   ShowHnCollector, VendorCustomerCollector,
                                   WeWorkRemotelyCollector, YcCollector)
from .collectors.edgar import EdgarCollector
from .collectors.producthunt import ProductHuntCollector
from .collectors.social import SocialCollector
from .collectors.statuspage import StatuspageCollector
from .collectors.techstack import TechstackCollector

log = logging.getLogger("scheduler")


def build(name: str):
    if name == "ats":
        return AtsCollector()
    if name == "techstack":
        return TechstackCollector()
    if name == "edgar":
        return EdgarCollector()
    if name == "hn_hiring":
        return HnHiringCollector()
    if name == "yc":
        return YcCollector()
    if name == "show_hn":
        return ShowHnCollector()
    if name == "wwr":
        return WeWorkRemotelyCollector()
    if name == "producthunt":
        return ProductHuntCollector()
    if name == "jobfeeds":
        return JobFeedCollector()
    if name == "vendors":
        return VendorCustomerCollector()
    if name == "statuspage":
        return StatuspageCollector()
    if name == "stargazers":
        return StargazerCollector()
    if name == "contacts":
        return ContactsCollector()
    if name in ("mastodon", "devto"):
        return CommunityCollector(name)
    if name in ("hn", "github", "lobsters", "reddit"):
        return SocialCollector(name)
    raise ValueError(f"unknown collector {name!r}")


def run_one(name: str) -> dict:
    db.init()
    res = run_collector(build(name))
    # Newly stored posts are cheap to classify right after their source runs.
    if name in ("hn", "github", "lobsters", "reddit", "mastodon", "devto"):
        res["llm"] = llm.classify_pending()
    return res


def start() -> BackgroundScheduler:
    from .config import cfg
    sched = BackgroundScheduler(timezone="UTC",
                                job_defaults={"coalesce": True, "max_instances": 1,
                                              "misfire_grace_time": 3600})
    for name, spec in cfg()["collectors"].items():
        if not spec.get("enabled"):
            log.info("collector %s disabled", name)
            continue
        # Do NOT pass next_run_time=None here — APScheduler reads that as
        # "paused" and the job silently never fires. Omitting it lets the
        # interval trigger schedule the first run one interval out, which also
        # avoids every collector stampeding at boot.
        sched.add_job(run_one, "interval", minutes=spec["every_minutes"],
                      args=[name], id=name, replace_existing=True)
    sched.start()
    for job in sched.get_jobs():
        log.info("scheduled %-10s every %-5s min  next=%s", job.id,
                 int(job.trigger.interval.total_seconds() // 60), job.next_run_time)
    return sched
