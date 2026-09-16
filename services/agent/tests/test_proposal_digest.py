import pytest

from app.domain.digest import canonical_json, proposal_digest

PROPOSAL = {
    "appointment_id": "slot-123",
    "doctor": "Dr Example",
    "time": "2026-09-19T18:30:00+05:30",
    "price": 800,
}


def test_key_order_does_not_change_the_digest() -> None:
    reordered = {
        "price": 800,
        "time": "2026-09-19T18:30:00+05:30",
        "doctor": "Dr Example",
        "appointment_id": "slot-123",
    }
    assert proposal_digest(reordered) == proposal_digest(PROPOSAL)


def test_nested_key_order_does_not_change_the_digest() -> None:
    first = {"patient": {"name": "A", "phone": "1"}, "slot": "x"}
    second = {"slot": "x", "patient": {"phone": "1", "name": "A"}}
    assert proposal_digest(first) == proposal_digest(second)


@pytest.mark.parametrize(
    "changed",
    [
        {**PROPOSAL, "price": 900},
        {**PROPOSAL, "doctor": "Dr Other"},
        {**PROPOSAL, "price": "800"},  # A different JSON type is a different proposal.
        {key: value for key, value in PROPOSAL.items() if key != "price"},
        {**PROPOSAL, "extra": None},
    ],
)
def test_any_meaningful_difference_changes_the_digest(changed: dict[str, object]) -> None:
    assert proposal_digest(changed) != proposal_digest(PROPOSAL)


def test_digest_is_lowercase_sha256_hex() -> None:
    digest = proposal_digest(PROPOSAL)
    assert len(digest) == 64
    assert set(digest) <= set("0123456789abcdef")


def test_canonical_json_is_compact_and_sorted() -> None:
    assert canonical_json({"b": 1, "a": [2, {"d": 3, "c": 4}]}) == '{"a":[2,{"c":4,"d":3}],"b":1}'


def test_canonical_json_keeps_non_ascii_as_characters() -> None:
    # Escaping would make the digest depend on the caller's encoder, not the value.
    assert canonical_json({"doctor": "Dr Ríos"}) == '{"doctor":"Dr Ríos"}'


def test_canonical_json_refuses_values_json_cannot_round_trip() -> None:
    with pytest.raises(ValueError):
        canonical_json({"price": float("nan")})
