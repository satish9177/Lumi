"""Run the appointment fixture as a standalone loopback site."""

import argparse

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8801)
    parser.add_argument("--log-level", default="warning")
    arguments = parser.parse_args()
    uvicorn.run(
        "evals.sites.appointments.app:create_site",
        factory=True,
        host=arguments.host,
        port=arguments.port,
        log_level=arguments.log_level,
    )


if __name__ == "__main__":
    main()
