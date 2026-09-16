"""Run the appointment fixture as a standalone loopback site."""

import argparse
import os
import threading
import time
from datetime import date

import uvicorn

from app.services.parent_watchdog import parent_liveness
from evals.sites.appointments.app import create_site
from evals.sites.appointments.state import AppointmentStore, anchored_catalogue


def _watch_parent(parent_pid: int) -> None:
    """Exit with the owning app, even after a hard kill that skips its quit hooks."""
    context = parent_liveness(parent_pid)
    is_alive = context.__enter__()

    def watch() -> None:
        while is_alive():
            time.sleep(1.0)
        os._exit(0)

    threading.Thread(target=watch, name="parent-watchdog", daemon=True).start()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8801)
    parser.add_argument("--log-level", default="warning")
    parser.add_argument(
        "--demo-dates",
        action="store_true",
        help="move the catalogue to the coming Saturday (packaged demo only)",
    )
    parser.add_argument(
        "--parent-pid",
        type=int,
        default=None,
        help="exit when this process exits (the packaged app that started the demo site)",
    )
    arguments = parser.parse_args()
    if arguments.parent_pid is not None:
        _watch_parent(arguments.parent_pid)
    if arguments.host != "127.0.0.1":
        parser.error("the clinic fixture binds to 127.0.0.1 only")
    store = (
        AppointmentStore(anchored_catalogue(date.today()))
        if arguments.demo_dates
        else AppointmentStore()
    )
    uvicorn.run(
        create_site(store),
        host=arguments.host,
        port=arguments.port,
        log_level=arguments.log_level,
    )


if __name__ == "__main__":
    main()
