"""The one LOCAL_DRAFT operation the worker registers (Milestone 8b S6).

This module only *declares* the operation. Every call that changes a page lives in
`app/browser/local_form_draft.py`, the one module the source scan allows to contain
one. The input model is `FillFormInput`, a strict shape built by the runtime from a
persisted, exactly-approved manifest: there is no selector, XPath, DOM id, URL,
origin, script, button or submit target in it, and no field to add one.

`retry = NEW_APPROVAL_REQUIRED`: a repeat is never automatic. The approval that
funded this dispatch was spent, and whether the draft it made still exists is
something only a fresh observation and a fresh exact approval can settle.
`reconciliation = NOT_REQUIRED`: there is no authoritative verifier for "did this
site save my draft"; the freeze is what makes the question moot.
"""

from app.browser.local_form_draft import run_fill
from app.browser.registry import (
    BrowserOperation,
    Effect,
    OperationTarget,
    Reconciliation,
    RetryPolicy,
)
from app.domain.local_form_draft import FILL_OPERATION, FillFormInput, FillResult

OPERATIONS: tuple[BrowserOperation, ...] = (
    BrowserOperation(
        name=FILL_OPERATION,
        description=(
            "Write the exactly-approved values into an authenticated form of a headed "
            "preparation window, with the network frozen at two layers, verify each value from "
            "the live DOM, and leave the page frozen. Three primitives only: set a text value, "
            "select an option, set a checkbox. No click, key press, upload, submit or "
            "navigation. Nothing is sent while Lumi fills."
        ),
        input_model=FillFormInput,
        output_model=FillResult,
        effect=Effect.LOCAL_DRAFT,
        retry=RetryPolicy.NEW_APPROVAL_REQUIRED,
        reconciliation=Reconciliation.NOT_REQUIRED,
        timeout_seconds=120.0,
        timeout_meaning=(
            "Fields may have been written into a browser that stayed frozen, so no request can "
            "have left. Whether a draft still exists is unknown; the runtime destroys the page "
            "and a new approval is required."
        ),
        preconditions=(
            "an exact, single-use approval of a form-prepare-v2 disclosure manifest funded a "
            "persisted attempt",
            "the profile is open headed in preparation mode and the freeze owner is this dispatch "
            "with both layers frozen and no request or relay in flight",
        ),
        postconditions=(
            "every field was re-derived from the live DOM and matched its approved identity",
            "every written value was read back from the DOM and equals the approved value",
            "the network is still frozen and the page is dirty until a discard or a handover",
            "no value, label, selector or page text is returned",
        ),
        handler=run_fill,
        target=OperationTarget.AUTHENTICATED_SESSION,
    ),
)

__all__ = ["OPERATIONS"]
