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
"""

import argparse

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser(description="Lumi isolated browser worker")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8802)
    parser.add_argument("--log-level", default="info")
    arguments = parser.parse_args()
    uvicorn.run(
        "app.browser.worker:create_worker_app",
        factory=True,
        host=arguments.host,
        port=arguments.port,
        log_level=arguments.log_level,
    )


if __name__ == "__main__":
    main()
