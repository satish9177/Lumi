"""Run the account fixture as a standalone loopback site."""

import argparse

import uvicorn

from evals.sites.account_fixture.app import create_site


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8821)
    parser.add_argument("--log-level", default="warning")
    arguments = parser.parse_args()
    if arguments.host != "127.0.0.1":
        parser.error("the account fixture binds to 127.0.0.1 only")
    uvicorn.run(create_site(), host=arguments.host, port=arguments.port, log_level=arguments.log_level)


if __name__ == "__main__":
    main()
