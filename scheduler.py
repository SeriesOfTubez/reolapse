#!/usr/bin/env python3
"""Portable build scheduler for container deployments.

This is the Docker equivalent of the systemd timers in deploy/: it runs the
daily build shortly after midnight, and the yearly rebuild after the daily
build on Sundays. On a Linux VM you can use the systemd units instead and skip
this process entirely.
"""

import datetime as dt
import logging
import subprocess
import sys
import time
from pathlib import Path

APP = Path(__file__).resolve().parent
log = logging.getLogger("scheduler")

DAILY_AT = dt.time(0, 10)   # build yesterday's video every day at this local time
YEARLY_WEEKDAY = 6          # 0=Mon .. 6=Sun: rebuild the yearly video this day


def run(*args):
    log.info("running build: %s", " ".join(args))
    try:
        subprocess.run([sys.executable, str(APP / "build_timelapse.py"), *args], check=True)
    except subprocess.CalledProcessError as exc:
        log.error("build %s failed: %s", args, exc)


def next_run(now):
    target = dt.datetime.combine(now.date(), DAILY_AT)
    if target <= now:
        target += dt.timedelta(days=1)
    return target


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    log.info("scheduler started; daily build at %s, yearly on weekday %d", DAILY_AT, YEARLY_WEEKDAY)
    while True:
        now = dt.datetime.now()
        target = next_run(now)
        log.info("next daily build at %s (%.0f min)", target, (target - now).total_seconds() / 60)
        time.sleep(max(1, (target - now).total_seconds()))
        run("daily")
        if dt.datetime.now().weekday() == YEARLY_WEEKDAY:
            run("yearly")


if __name__ == "__main__":
    main()
