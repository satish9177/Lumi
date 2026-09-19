"""Bounded, quiescence-aware teardown for the browser-worker test harnesses.

Every browser-marked test module that opens its own `EgressBroker` and its
own `Playwright` driver per test -- `test_egress_broker_browser.py` (S0),
`test_browser_profile_session.py` / `test_browser_profile_worker.py` (S1),
and `test_login_takeover_browser.py` / `test_login_takeover_service_browser.py`
/ `test_takeover_network.py` (S2) -- shares the same fixture teardown shape:

```python
finally:
    await playwright.stop()
    await broker.aclose()
    await site.aclose()   # and any other fixture server
```

A hang was observed (Windows only) in `playwright.stop()`, reproducible
across *all* of these files once run enough times in a row, not just the S2
ones -- confirming this is a latent property of the teardown shape itself,
pre-existing since S0, not something Milestone 8a S2 introduced. It simply
runs headed Chromium (S2's own addition) often enough to hit it reliably.

Consistent with S1's own measured finding that full Chromium keeps an idle
keep-alive connection to the proxy open for a few seconds *after* the page
that used it has already closed (`docs/reviews/milestone-8-s1.md` §16),
tearing down the Playwright driver while a child Chromium process still has
such a connection lingering appears to be the trigger. `quiesce_broker` waits
for the broker to report zero active connections (the same signal M8b's
freeze entry will need) before anything is torn down, and `bounded` makes
every step fail loud and fast instead of hanging the whole suite if that
turns out not to be sufficient.

This is a mitigation applied at the *test-infrastructure* layer, not a fix
to any production module (`app/browser/egress_broker.py` and friends are
untouched), and not a fully isolated root cause: the exact mechanism inside
`playwright.stop()`'s Windows I/O completion port wait was not traced
further. Treat a `bounded()` timeout as a signal worth investigating, not as
an expected outcome.
"""

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any, TypeVar

from app.browser.egress_broker import EgressBroker

logger = logging.getLogger("tests.broker_teardown")

T = TypeVar("T")

DEFAULT_STEP_TIMEOUT_SECONDS = 20.0
DEFAULT_QUIESCE_TIMEOUT_SECONDS = 10.0


async def quiesce_broker(broker: EgressBroker, timeout: float = DEFAULT_QUIESCE_TIMEOUT_SECONDS) -> int:
    """Wait until the broker holds no relay, and say how many it held.

    Mirrors `test_egress_broker_browser.py::quiesced`. A real freeze entry
    has to drive this to zero before it may claim nothing was in flight;
    here it is used defensively, before tearing down the driver that owns
    the Chromium processes those relays belong to.
    """
    deadline = asyncio.get_running_loop().time() + timeout
    while broker.active_connections and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.1)
    return broker.active_connections


async def bounded(step_name: str, coroutine: Coroutine[Any, Any, T], *, timeout: float = DEFAULT_STEP_TIMEOUT_SECONDS) -> T | None:
    """Run one teardown step; log and move on rather than hang forever.

    A timeout here means this step did not complete -- possibly leaving a
    resource (a broker socket, a driver process) not cleanly released. That
    is a worse outcome than a clean teardown, and a better one than taking
    the rest of the test suite down with it.
    """
    try:
        return await asyncio.wait_for(coroutine, timeout=timeout)
    except TimeoutError:
        logger.warning("teardown step %r did not complete within %.0fs; abandoning it", step_name, timeout)
        return None


__all__ = ["DEFAULT_QUIESCE_TIMEOUT_SECONDS", "DEFAULT_STEP_TIMEOUT_SECONDS", "bounded", "quiesce_broker"]
