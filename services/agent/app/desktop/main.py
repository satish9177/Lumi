"""Entrypoint for the desktop worker process.

Started only by the runtime's supervisor, with a fixed argument vector and an
environment built from an allowlist. It binds port 0 itself and reports the port it
actually owns as one JSON line on a pipe only the supervisor holds. The supervisor connects
only to that reported port, so no other local listener can win a port race and be handed the
credential (which it does not have: the credential is in this process's own environment and is
sent by the supervisor only to the address the worker reported).

The worker never receives `DATABASE_URL`, a provider key, a browser profile path, task
history, a shell, an executable path or Python to run.
"""

import argparse
import asyncio
import json
import os
from contextlib import AbstractContextManager
from socket import socket

import uvicorn

from app.desktop.protocol import READY_EVENT
from app.services.parent_watchdog import ParentLiveness, parent_liveness, watch_liveness


class _ReadyServer(uvicorn.Server):
    def __init__(self, config: uvicorn.Config, *, parent_pid: int | None) -> None:
        super().__init__(config)
        self._parent_pid = parent_pid
        self._watchdog: asyncio.Task[None] | None = None
        # Retained: collecting the context manager would close the handle.
        self._parent: AbstractContextManager[ParentLiveness] | None = None

    async def startup(self, sockets: list[socket] | None = None) -> None:
        await super().startup(sockets=sockets)
        if not self.started or self.should_exit:
            raise RuntimeError("desktop worker stopped before binding")
        if self._parent_pid is not None:
            self._parent = parent_liveness(self._parent_pid)
            is_alive = self._parent.__enter__()
            if not is_alive():
                raise RuntimeError("the owning runtime is not running")
            self._watchdog = asyncio.create_task(watch_liveness(is_alive))
        port = self.servers[0].sockets[0].getsockname()[1]
        os.write(1, (json.dumps({"event": READY_EVENT, "port": port}) + "\n").encode("ascii"))
        # Nothing else may ever be written to the readiness pipe.
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, 1)
        os.close(devnull)


def main() -> None:
    parser = argparse.ArgumentParser(description="Lumi isolated desktop worker")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--log-level", default="warning")
    parser.add_argument("--ready-stdout", action="store_true", required=True)
    arguments = parser.parse_args()
    if arguments.host != "127.0.0.1":
        parser.error("the desktop worker binds only 127.0.0.1")
    raw_parent = os.environ.get("LUMI_DESKTOP_PARENT_PID")
    parent_pid = int(raw_parent) if raw_parent else None
    config = uvicorn.Config(
        "app.desktop.worker:create_worker_app",
        factory=True,
        host=arguments.host,
        port=arguments.port,
        log_level=arguments.log_level,
        access_log=False,
    )
    _ReadyServer(config, parent_pid=parent_pid).run()


if __name__ == "__main__":
    main()
