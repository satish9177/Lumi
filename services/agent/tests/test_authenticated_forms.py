"""The value-free form inventory (Milestone 8b S4): schema, persistence, migration.

**S4 observes form structure only.** Three things are proven here without a
browser:

* the projection *cannot express* a value, a selector, an option value or any DOM
  identity (`extra="forbid"`, sequential bounded refs, redaction re-checked);
* observation schema versions are explicit: a stored v1 (S3) row stays v1, is
  still readable after migration `0009`, and can never be given an inventory; a v2
  (S4) row round-trips its inventory;
* the inventory is account-private local evidence that reaches no provider
  surface, no public research table and no diagnostics.
"""

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from app.api.authenticated_schemas import AuthenticatedObservationResponse
from app.config import Settings
from app.domain.authenticated import AuthenticatedObservation, AuthOperation
from app.domain.authenticated_forms import (
    EMPTY_INVENTORY,
    ElementProjection,
    FormInventory,
    FormProjection,
    FrameProjection,
    OptionProjection,
)
from app.domain.research import TextBlock, compute_content_hash
from app.repositories.authenticated import AuthenticatedObservationRecord, _observation
from app.services.authenticated_read import _step_summary
from tests.conftest import downgrade, migrate

MARKER = "FORM_FIELD_SECRET_MARKER_0AD"


def element(n: int = 1, **overrides: Any) -> ElementProjection:
    base: dict[str, Any] = {
        "element_ref": f"e{n}", "form_ref": "f1", "frame_ref": "fr0", "role": "textbox",
        "control_type": "text", "accessible_name": "Full legal name", "value_state": "filled",
        "required": True,
    }
    return ElementProjection(**{**base, **overrides})


def inventory(*elements: ElementProjection, **overrides: Any) -> FormInventory:
    base: dict[str, Any] = {
        "forms": [FormProjection(ref="f1", label="Application")],
        "frames": [FrameProjection(ref="fr0")],
        "elements": list(elements),
    }
    return FormInventory(**{**base, **overrides})


def observation(*, version: int = 2, form: FormInventory | None = None, epoch: int = 1,
                sequence: int = 1, kind: str = "page") -> AuthenticatedObservation:
    blocks = [TextBlock(id="b1", text="Application")] if kind == "page" else []
    return AuthenticatedObservation(
        schema_version=version, observation_id=uuid.uuid4(), kind=kind, operation=AuthOperation.OBSERVE,
        sequence=sequence, profile_id=uuid.uuid4(), tab="t1", document_epoch=1,
        host="example.test" if kind == "page" else None, title="Apply", settled=True,
        observed_at=datetime.now(UTC), blocks=blocks, open_tabs=["t1"], total_text_chars=11,
        content_hash=compute_content_hash(
            kind=kind, final_url="example.test" if kind == "page" else "", title="Apply",
            blocks=blocks, links=[], results=[],
        ),
        form_epoch=epoch if version == 2 else 0,
        inventory=form if form is not None else EMPTY_INVENTORY,
    )


# ---- the projection cannot express a leak ------------------------------------------------


@pytest.mark.parametrize(
    "extra",
    [
        {"value": "Satish"}, {"default_value": "x"}, {"value_preview": "Sat"}, {"name": "n"},
        {"id": "i"}, {"class": "c"}, {"tag_name": "input"}, {"selector": "#a"}, {"xpath": "//a"},
        {"dom_path": "a>b"}, {"html": "<a>"}, {"outer_html": "<a>"}, {"event_handler": "x()"},
        {"bounding_box": {"x": 1}}, {"coordinates": [1, 2]}, {"dataset": {}}, {"action": "/x"},
        {"method": "post"}, {"autocomplete": "name"}, {"placeholder": "p"}, {"frame_url": "http://a"},
    ],
)
def test_an_element_rejects_every_field_it_must_never_carry(extra: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        element(**extra)


@pytest.mark.parametrize("extra", [{"value": "India"}, {"selected": True}, {"id": "x"}])
def test_an_option_carries_only_a_ref_and_a_label(extra: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        OptionProjection(ref="op1", label="India", **extra)


@pytest.mark.parametrize("extra", [{"id": "x"}, {"name": "n"}, {"action": "/a"}, {"method": "post"}])
def test_a_form_and_a_frame_carry_no_identity(extra: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        FormProjection(ref="f1", **extra)
    with pytest.raises(ValidationError):
        FrameProjection(ref="fr0", **{**extra, "url": "http://a"})


def test_the_element_field_set_is_exactly_the_reviewed_one() -> None:
    assert set(ElementProjection.model_fields) == {
        "element_ref", "form_ref", "frame_ref", "role", "control_type", "accessible_name",
        "label_ref", "value_state", "required", "enabled", "visible", "read_only", "max_length",
        "option_refs", "submit_like",
    }


def test_value_state_is_only_ever_empty_filled_or_unknown() -> None:
    for state in ("empty", "filled", "unknown"):
        assert element(value_state=state).value_state == state
    with pytest.raises(ValidationError):
        element(value_state="Satish")
    # Ambiguous controls cannot claim a state.
    with pytest.raises(ValidationError):
        element(role="checkbox", control_type="checkbox", value_state="filled")
    with pytest.raises(ValidationError):
        element(role="combobox", control_type="select_single", value_state="empty")


@pytest.mark.parametrize(
    "bad",
    [
        {"element_ref": "e0"}, {"element_ref": "e41"}, {"element_ref": "x1"}, {"form_ref": "f6"},
        {"form_ref": "f0"}, {"frame_ref": "fr5"}, {"frame_ref": "fr"}, {"role": "div"},
        {"control_type": "password"}, {"control_type": "file"}, {"accessible_name": "x" * 121},
        {"max_length": -1},
    ],
)
def test_refs_roles_and_bounds_are_closed(bad: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        element(**bad)


def test_only_a_select_or_radio_group_carries_options_and_only_a_button_is_submit_like() -> None:
    options = [OptionProjection(ref="op1", label="India")]
    element(role="combobox", control_type="select_single", value_state="unknown", option_refs=options)
    with pytest.raises(ValidationError):
        element(option_refs=options)
    with pytest.raises(ValidationError):
        element(submit_like=True)
    with pytest.raises(ValidationError):
        element(role="button", control_type="submit_like", value_state="unknown", submit_like=False)
    with pytest.raises(ValidationError):
        element(role="combobox", control_type="select_single", value_state="unknown",
                option_refs=[OptionProjection(ref="op2", label="India")])


def test_option_and_element_counts_are_bounded() -> None:
    twenty_five = [OptionProjection(ref=f"op{n}", label=f"C{n}") for n in range(1, 26)]
    element(role="combobox", control_type="select_single", value_state="unknown", option_refs=twenty_five)
    with pytest.raises(ValidationError):
        OptionProjection(ref="op26", label="C26")
    with pytest.raises(ValidationError):
        element(role="combobox", control_type="select_single", value_state="unknown",
                option_refs=[*twenty_five, OptionProjection(ref="op25", label="dup")])
    forty = [element(n) for n in range(1, 41)]
    inventory(*forty)
    with pytest.raises(ValidationError):
        inventory(*forty, element(40))  # a forty-first element
    with pytest.raises(ValidationError):
        inventory(forms=[FormProjection(ref=f"f{n}") for n in range(1, 6)] + [FormProjection(ref="f5")])
    with pytest.raises(ValidationError):
        inventory(frames=[FrameProjection(ref=f"fr{n}") for n in range(0, 5)] + [FrameProjection(ref="fr4")])


def test_refs_must_be_sequential_and_must_name_a_real_form_and_frame() -> None:
    with pytest.raises(ValidationError):
        inventory(element(2))
    with pytest.raises(ValidationError):
        inventory(element(1), element(3))
    with pytest.raises(ValidationError):
        inventory(element(1, form_ref="f2"))
    with pytest.raises(ValidationError):
        inventory(element(1, frame_ref="fr1"))
    with pytest.raises(ValidationError):
        inventory(frames=[FrameProjection(ref="fr1")])
    with pytest.raises(ValidationError):
        inventory(forms=[FormProjection(ref="f2")])


@pytest.mark.parametrize(
    "unredacted", ["Reply to satish@example.test", "Customer 123456789012345", "Card 4242424242424242"]
)
def test_a_name_or_label_that_would_still_change_under_redaction_is_refused(unredacted: str) -> None:
    with pytest.raises(ValidationError):
        inventory(element(1, accessible_name=unredacted))
    with pytest.raises(ValidationError):
        inventory(element(1, role="combobox", control_type="select_single", value_state="unknown",
                          option_refs=[OptionProjection(ref="op1", label=unredacted)]))
    with pytest.raises(ValidationError):
        inventory(element(1), forms=[FormProjection(ref="f1", label=unredacted)])


# ---- observation versions ------------------------------------------------------------------


def test_a_v2_observation_carries_its_inventory_and_form_epoch() -> None:
    seen = observation(form=inventory(element(1)), epoch=3)
    assert (seen.schema_version, seen.form_epoch, seen.inventory.element_count) == (2, 3, 1)


def test_a_v1_observation_can_never_have_an_inventory() -> None:
    assert observation(version=1).inventory == EMPTY_INVENTORY
    with pytest.raises(ValidationError):
        observation(version=1, form=inventory(element(1)))
    with pytest.raises(ValidationError):
        AuthenticatedObservation(**{**observation(version=1).model_dump(), "form_epoch": 2})
    with pytest.raises(ValidationError):
        observation(version=3)


def test_a_tab_state_observation_has_no_elements() -> None:
    assert observation(kind="tab_state").inventory.element_count == 0
    with pytest.raises(ValidationError):
        observation(kind="tab_state", form=inventory(element(1)))


def test_the_api_response_shape_is_still_text_and_links_only() -> None:
    fields = set(AuthenticatedObservationResponse.model_fields)
    assert "inventory" not in fields and "form_epoch" not in fields and "elements" not in fields
    record = AuthenticatedObservationRecord(
        observation=observation(form=inventory(element(1, accessible_name=MARKER))),
        task_id=uuid.uuid4(), grant_id=uuid.uuid4(), profile_id=uuid.uuid4(), action_id=uuid.uuid4(),
        attempt_id=uuid.uuid4(), dispatch_id=None, worker_generation=None, created_at=datetime.now(UTC),
    )
    response = AuthenticatedObservationResponse.from_record(record)
    assert response.schema_version == 1  # the response shape, not the stored version
    assert MARKER not in response.model_dump_json()


def test_diagnostics_carry_counts_and_the_epoch_and_never_a_name() -> None:
    from app.services.browser_execution import Outcome
    from app.domain.action_status import AttemptOutcome
    from app.domain.browser_dispatch import DispatchStatus

    seen = observation(form=inventory(element(1, accessible_name=MARKER), element(2, accessible_name="Other")))
    outcome = Outcome(
        outcome=AttemptOutcome.SUCCEEDED, dispatch_status=DispatchStatus.OK, submitted=True,
        error_code=None, observation_id=seen.observation_id, result={}, observation={},
    )
    summary = _step_summary(outcome, seen)
    assert summary["form_count"] == 1 and summary["element_count"] == 2
    assert summary["option_count"] == 0 and summary["form_epoch"] == 1
    assert summary["inventory_truncated"] is False
    assert MARKER not in repr(summary) and "Other" not in repr(summary)


# ---- persistence and migration 0009 -------------------------------------------------------------


async def _insert_row(connection: Any, ids: dict[str, uuid.UUID], *, sequence: int, version: int,
                      epoch: int | None = None, inventory_json: str | None = None) -> None:
    """Place one evidence row without building a task, grant and attempt around it.

    `session_replication_role = replica` disables foreign-key enforcement for this
    connection only. Columns S4 added are written only when given, so a version-1
    row is exactly what the S3 code wrote.
    """
    await connection.execute(text("SET LOCAL session_replication_role = replica"))
    columns = ""
    values = ""
    params: dict[str, Any] = {
        "id": uuid.uuid4(), "task": ids["task"], "grant": ids["grant"], "profile": ids["profile"],
        "action": ids["action"], "attempt": uuid.uuid4(), "sequence": sequence, "version": version,
        "hash": compute_content_hash(
            kind="page", final_url="example.test", title="Old", blocks=[], links=[], results=[]
        ),
    }
    if epoch is not None:
        columns += ", form_epoch"
        values += ", :epoch"
        params["epoch"] = epoch
    if inventory_json is not None:
        columns += ", element_inventory"
        values += ", CAST(:inventory AS jsonb)"
        params["inventory"] = inventory_json
    await connection.execute(
        text(
            "INSERT INTO authenticated_observations (id, task_id, grant_id, profile_id, action_id, "
            "attempt_id, sequence, schema_version, classification, provenance, kind, operation, tab, "
            f"document_epoch, host, title, settled, truncated, observed_at, content_hash, projection{columns}) "
            "VALUES (:id, :task, :grant, :profile, :action, :attempt, :sequence, :version, "
            "'account_private', 'untrusted_environment', 'page', 'observe', 't1', 1, 'example.test', "
            "'Old', true, false, now(), :hash, CAST(:projection AS jsonb)"
            f"{values})"
        ),
        {
            **params,
            "projection": '{"blocks": [], "links": [], "open_tabs": ["t1"], "total_text_chars": 0, '
            '"total_link_count": 0, "redactions": {}}',
        },
    )


async def test_migration_0009_keeps_existing_v1_rows_readable_and_v1(
    settings: Settings, migrated_database_url: str
) -> None:
    from app.db.engine import create_database_engine

    await asyncio.to_thread(downgrade, migrated_database_url, "0008")
    ids = {name: uuid.uuid4() for name in ("task", "grant", "profile", "action", "attempt")}
    engine = create_database_engine(settings)
    try:
        async with engine.begin() as connection:
            await _insert_row(connection, ids, sequence=1, version=1)
        await asyncio.to_thread(migrate, migrated_database_url)
        async with engine.begin() as connection:
            await connection.execute(text("SET LOCAL session_replication_role = replica"))
            row = (await connection.execute(text("SELECT * FROM authenticated_observations"))).one()
            assert (row.schema_version, row.form_epoch, row.element_inventory) == (1, 0, {})
            stored = _observation(row)
            assert stored.observation.schema_version == 1
            assert stored.observation.inventory == EMPTY_INVENTORY and stored.observation.form_epoch == 0
        # Historical evidence was not rewritten, and cannot be (a fresh connection,
        # because replica mode also switches the immutability trigger off).
        with pytest.raises(Exception, match="immutable evidence"):
            async with engine.begin() as connection:
                await connection.execute(text("UPDATE authenticated_observations SET title = 'x'"))
    finally:
        await engine.dispose()
        await asyncio.to_thread(migrate, migrated_database_url)
        await _cleanup(settings)


async def _cleanup(settings: Settings) -> None:
    from app.db.engine import create_database_engine

    engine = create_database_engine(settings)
    try:
        async with engine.begin() as connection:
            await connection.execute(text("SET LOCAL session_replication_role = replica"))
            await connection.execute(text("DELETE FROM authenticated_observations"))
    finally:
        await engine.dispose()


async def test_a_v2_row_round_trips_its_inventory_and_v1_cannot_hold_one(settings: Settings) -> None:
    from app.db.engine import create_database_engine

    ids = {name: uuid.uuid4() for name in ("task", "grant", "profile", "action", "attempt")}
    engine = create_database_engine(settings)
    inventory_json = inventory(element(1, accessible_name=MARKER)).model_dump_json()
    try:
        async with engine.begin() as connection:
            await _insert_row(
                connection, ids, sequence=1, version=2, epoch=4, inventory_json=inventory_json
            )
        async with engine.connect() as connection:
            await connection.execute(text("SET LOCAL session_replication_role = replica"))
            row = (await connection.execute(text("SELECT * FROM authenticated_observations"))).one()
            stored = _observation(row)
        assert stored.observation.schema_version == 2 and stored.observation.form_epoch == 4
        assert [e.accessible_name for e in stored.observation.inventory.elements] == [MARKER]
        # "Version 1 with an inventory" is unrepresentable in the database, too.
        with pytest.raises(IntegrityError):
            async with engine.begin() as connection:
                await _insert_row(
                    connection, ids, sequence=2, version=1, epoch=4, inventory_json=inventory_json
                )
        with pytest.raises(IntegrityError):
            async with engine.begin() as connection:
                await _insert_row(connection, ids, sequence=3, version=1, inventory_json=inventory_json)
        with pytest.raises(IntegrityError):
            async with engine.begin() as connection:
                await _insert_row(connection, ids, sequence=4, version=3)
    finally:
        await engine.dispose()
        await _cleanup(settings)


async def test_the_migration_adds_only_the_two_columns_and_no_s5_or_s6_state(engine: AsyncEngine) -> None:
    async with engine.connect() as connection:
        columns = {
            row[0]
            for row in await connection.execute(
                text("SELECT column_name FROM information_schema.columns WHERE table_name = 'authenticated_observations'")
            )
        }
        tables = {
            row[0]
            for row in await connection.execute(
                text("SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'")
            )
        }
    assert {"form_epoch", "element_inventory"} <= columns
    for absent in ("frozen_at", "manifest_digest", "written_value_hash", "protected_value"):
        assert absent not in columns
    for absent in ("protected_values", "form_drafts", "disclosure_manifests"):
        assert absent not in tables


async def test_the_downgrade_leaves_a_structurally_correct_v1_schema(
    settings: Settings, migrated_database_url: str
) -> None:
    from app.db.engine import create_database_engine

    ids = {name: uuid.uuid4() for name in ("task", "grant", "profile", "action", "attempt")}
    engine = create_database_engine(settings)
    inventory_json = inventory(element(1)).model_dump_json()
    try:
        async with engine.begin() as connection:
            await _insert_row(connection, ids, sequence=1, version=1)
            await _insert_row(
                connection, ids, sequence=2, version=2, epoch=1, inventory_json=inventory_json
            )
        await asyncio.to_thread(downgrade, migrated_database_url, "0008")
        async with engine.connect() as connection:
            remaining = (await connection.execute(text("SELECT schema_version FROM authenticated_observations"))).all()
            columns = {
                row[0] for row in await connection.execute(
                    text("SELECT column_name FROM information_schema.columns WHERE table_name = 'authenticated_observations'")
                )
            }
        assert [row[0] for row in remaining] == [1]
        assert "form_epoch" not in columns and "element_inventory" not in columns
    finally:
        await engine.dispose()
        await asyncio.to_thread(migrate, migrated_database_url)
        await _cleanup(settings)
