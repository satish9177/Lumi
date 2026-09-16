"""Entrypoint for the browser worker process.

Run it separately from the runtime, with only the variables it needs:

    $env:LUMI_BROWSER_TOKEN = "<minted by trusted bootstrap code>"
    $env:LUMI_BROWSER_ALLOWED_ORIGINS = "appointment_fixture=http://127.0.0.1:8801"
    $env:LUMI_BROWSER_HEADLESS = "false"   # headed, for a local demonstration
    uv run python -m app.browser.main --port 8802

Running it as its own process is the isolation. A worker embedded in the runtime
would share the runtime's memory, its database connections and its environment,
and every claim in this milestone about least privilege would become a claim
about programmer discipline instead.

When the runtime manages the worker (`--ready-stdout`), the worker binds port 0
itself and reports the port it actually owns as one JSON line on stdout, which
is a pipe only the runtime holds. The runtime sends the worker credential only
after that, so no other local listener can win a port race and receive it.
"""

import argparse
import asyncio
import json
import os
from contextlib import AbstractContextManager
from socket import socket

import uvicorn

from app.services.parent_watchdog import ParentLiveness, parent_liveness, watch_liveness

READY_EVENT = "lumi-worker-ready"


class _ReadyServer(uvicorn.Server):
    def __init__(self, config: uvicorn.Config, *, ready_stdout: bool, parent_pid: int | None) -> None:
        super().__init__(config)
        self._ready_stdout = ready_stdout
        self._parent_pid = parent_pid
        self._watchdog: asyncio.Task[None] | None = None
        # Retained: collecting the context manager would close the handle.
        self._parent: AbstractContextManager[ParentLiveness] | None = None

    async def startup(self, sockets: list[socket] | None = None) -> None:
        await super().startup(sockets=sockets)
        if not self.started or self.should_exit:
            raise RuntimeError("browser worker stopped before binding")
        if self._parent_pid is not None:
            self._parent = parent_liveness(self._parent_pid)
            is_alive = self._parent.__enter__()
            if not is_alive():
                raise RuntimeError("the owning runtime is not running")
            # The handle stays open for the process lifetime on purpose.
            self._watchdog = asyncio.create_task(watch_liveness(is_alive))
        if not self._ready_stdout:
            return
        port = self.servers[0].sockets[0].getsockname()[1]
        os.write(1, (json.dumps({"event": READY_EVENT, "port": port}) + "\n").encode("ascii"))
        # Nothing else may ever be written to the readiness pipe.
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, 1)
        os.close(devnull)


def main() -> None:
    parser = argparse.ArgumentParser(description="Lumi isolated browser worker")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8802)
    parser.add_argument("--log-level", default="info")
    parser.add_argument("--ready-stdout", action="store_true")
    arguments = parser.parse_args()
    if arguments.ready_stdout and arguments.host != "127.0.0.1":
        parser.error("a managed worker binds only 127.0.0.1")
    raw_parent = os.environ.get("LUMI_BROWSER_PARENT_PID")
    parent_pid = int(raw_parent) if raw_parent else None
    config = uvicorn.Config(
        "app.browser.worker:create_worker_app",
        factory=True,
        host=arguments.host,
        port=arguments.port,
        log_level=arguments.log_level,
        access_log=False,
    )
    _ReadyServer(config, ready_stdout=arguments.ready_stdout, parent_pid=parent_pid).run()


if __name__ == "__main__":
    main()
