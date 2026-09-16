import hashlib
import json
from typing import Any


def canonical_json(proposal: dict[str, Any]) -> str:
    """One byte-stable encoding of a proposal.

    Keys sorted, no insignificant whitespace, non-ASCII kept as characters so the
    encoding does not depend on the caller's escaping. Two proposals with the
    same meaning therefore always produce the same bytes, and the digest is a
    stable identity for an approval to bind to.
    """
    return json.dumps(
        proposal, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def proposal_digest(proposal: dict[str, Any]) -> str:
    """SHA-256 over the canonical encoding, computed by the server only.

    A digest supplied by an API caller is never trusted or stored: it would let
    a caller bind an approval to a proposal the server never saw.
    """
    return hashlib.sha256(canonical_json(proposal).encode("utf-8")).hexdigest()
