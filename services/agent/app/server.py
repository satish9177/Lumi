"""Fixed loopback entry point used by the trusted Electron supervisor."""

import argparse
import json
import os
from socket import socket

import uvicorn

from app.main import create_app
from app.services.windows_job import acquire_runtime_process_lock, configure_runtime_process_tree


class _ReadyServer(uvicorn.Server):
    async def startup(self, sockets: list[socket] | None = None) -> None:
        await super().startup(sockets=sockets)
        if not self.started or self.should_exit:
            raise RuntimeError("runtime stopped before binding")
        descriptor = os.environ.get("LUMI_RUNTIME_READY_FD")
        if descriptor is None:
            return
        if descriptor != "3":
            raise RuntimeError("invalid runtime readiness descriptor")
        # This dedicated inherited pipe is not stdout/stderr. Electron reads one
        # bounded JSON line only after uvicorn owns the loopback listener, so it
        # never sends the bearer credential to a guessed free port.
        with os.fdopen(3, "w", encoding="ascii", closefd=True) as ready:
            ready.write(json.dumps({"event": "lumi-runtime-ready", "port": self.config.port}))
            ready.write("\n")
            ready.flush()


def main() -> None:
    parser = argparse.ArgumentParser(description="Lumi local agent runtime")
    parser.add_argument("--port", type=int, required=True)
    arguments = parser.parse_args()
    if not 1 <= arguments.port <= 65_535:
        parser.error("--port must be between 1 and 65535")

    acquire_runtime_process_lock()
    configure_runtime_process_tree()
    application = create_app()
    config = uvicorn.Config(
        application,
        host="127.0.0.1",
        port=arguments.port,
        access_log=False,
        log_level="warning",
    )
    server = _ReadyServer(config)
    application.state.request_shutdown = lambda: setattr(server, "should_exit", True)
    server.run()


if __name__ == "__main__":
    main()
