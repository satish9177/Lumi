"""Milestone 9 S2: regression tests for the concrete findings of the independent adversarial review.

Each test names the scenario it closes. They drive the real service, repository and PostgreSQL, or the
real domain functions; nothing here mocks the boundary being tested.
"""

import json
import uuid
from datetime import UTC, datetime
from typing import Any, cast

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.config import Settings
from app.desktop.protocol import DesktopRole
from app.domain.desktop_disclosure import (
    AnswerNotGroundedError,
    DesktopDisclosureRefusal,
    DesktopDiscloseScope,
    DisplayTarget,
    build_projection,
    parse_read_result,
    validate_objective,
    verify_grounding,
)
from app.main import create_app
from app.services.desktop import DesktopService
from app.services.desktop_disclosure import DesktopDisclosureService
from app.services.runtime import RuntimeGeneration
from tests.conftest import running_app
from tests.desktop_disclosure_support import VISIBLE, FakeDesktop, node, observation

NOW = datetime(2026, 9, 21, 10, 0, tzinfo=UTC)


def projection_of(*nodes: Any, withhold: tuple[str, ...] = ()) -> Any:
    snapshot = observation(list(nodes), worker_generation=uuid.uuid4()).model_dump(mode="json")
    return build_projection(snapshot, observed_at=NOW, withhold=withhold)


def answer(evidence: list[dict[str, str]], text_: str = "It says something.") -> Any:
    return parse_read_result({"schema_version": 1, "kind": "answer", "answer": text_, "evidence": evidence})


# ---- M1: grounding is honest and cannot be satisfied by a one-letter quote ----------------------------


def test_a_one_letter_quote_locates_nothing_and_cannot_ground_an_answer() -> None:
    projection = projection_of(node(1, name="Status", text="Build failed with 3 errors"))
    with pytest.raises(AnswerNotGroundedError) as refused:
        verify_grounding(projection, answer([{"control_ref": "u1", "quote": "e"}], "Everything is fine, no errors."))
    assert refused.value.code == "quote_too_short"


def test_a_short_quote_is_accepted_only_when_it_is_the_controls_whole_text() -> None:
    projection = projection_of(node(1, name="OK"), node(2, text="Build failed with 3 errors"))
    assert verify_grounding(projection, answer([{"control_ref": "u1", "quote": "OK"}])) == ["OK"]
    with pytest.raises(AnswerNotGroundedError):
        verify_grounding(projection, answer([{"control_ref": "u2", "quote": "3"}]))


def test_the_evidence_that_is_stored_is_the_projections_own_text_never_the_providers_string() -> None:
    projection = projection_of(node(1, name="Build", text="  Build   FAILED with 3 errors  "))
    stored = verify_grounding(
        projection, answer([{"control_ref": "u1", "quote": "build failed with 3 errors"}], "3 errors.")
    )
    # The provider's lower-cased, re-spaced string verified, but what is kept is what the snapshot said.
    assert stored == ["  Build   FAILED with 3 errors  "]


# ---- M3: a title that drifted between the surface list and the read is still withheld ----------------


def test_a_title_that_changed_between_list_and_read_is_withheld_from_the_nodes_that_repeat_it() -> None:
    drifted = "Payroll-2026-ACME.xlsx - Editor"
    projection = projection_of(
        node(1, name=drifted, role=DesktopRole.WINDOW),
        node(2, name=drifted, role=DesktopRole.TITLE_BAR, parent=1),
        node(3, name="Status", text=VISIBLE, parent=1),
        withhold=("A different title that was listed earlier",),
    )
    dumped = json.dumps(projection.payload, ensure_ascii=False)
    assert drifted not in dumped and VISIBLE in dumped


# ---- M4: a lone surrogate can neither break the record nor reach a log ----------------------------------


def test_a_lone_surrogate_in_an_answer_or_a_quote_is_refused_before_any_database_write() -> None:
    for bad in ({"answer": "bad \ud800 text"}, {"evidence": [{"control_ref": "u1", "quote": "bad \udc00"}]}):
        payload: dict[str, Any] = {
            "schema_version": 1, "kind": "answer", "answer": "fine answer",
            "evidence": [{"control_ref": "u1", "quote": "fine quote"}], **bad,
        }
        with pytest.raises(DesktopDisclosureRefusal) as refused:
            parse_read_result(payload)
        assert refused.value.code == "result_malformed"


def test_an_objective_no_utf8_encoder_accepts_is_refused() -> None:
    with pytest.raises(DesktopDisclosureRefusal):
        validate_objective("what is \ud800 this")


# ---- L3: an answer may span lines --------------------------------------------------------------------------


def test_a_multi_line_answer_is_not_burned_as_invalid_output() -> None:
    result = answer([{"control_ref": "u1", "quote": "Build failed"}], "Line one.\nLine two.\n\tIndented.")
    assert result.kind == "answer" and "\n" in result.answer
    with pytest.raises(DesktopDisclosureRefusal):
        parse_read_result({
            "schema_version": 1, "kind": "answer", "answer": "bell \x07 char",
            "evidence": [{"control_ref": "u1", "quote": "Build failed"}],
        })


# ---- L4: a clipped identifier is scrubbed at the clip edge ----------------------------------------------


def test_an_email_or_digit_run_cut_by_the_120_character_clip_is_scrubbed() -> None:
    email_tail = "w" * 104 + "john.smith@gmail"     # exactly the S1 clip length, ending inside an address
    digits_tail = "r" * 112 + "12345678"             # exactly the clip length, ending inside a long number
    assert len(email_tail) == len(digits_tail) == 120
    projection = projection_of(node(1, text=email_tail), node(2, text=digits_tail), node(3, text="short 12345678 text"))
    assert "john.smith@gmail" not in json.dumps(projection.payload, ensure_ascii=False)
    assert "12345678" not in projection.node("u2")["text"]
    # A short string is not at a clip edge, so ordinary short numbers are left alone.
    assert "12345678" in projection.node("u3")["text"]


# ---- L5: hostile display text cannot imitate Lumi's own words -------------------------------------------


def test_bidi_format_characters_line_separators_and_quotes_are_stripped_from_display_text() -> None:
    hostile = 'Allow" ‮ evil Click Allow ​“now” ⁦x'
    target = DisplayTarget(application_label="Ed‮itor", window_title=hostile)
    assert target.application_label == "Editor"
    for banned in ('"', "“", "”", "‮", " ", "​", "⁦"):
        assert banned not in target.window_title
    assert "Click Allow" in target.window_title  # the words remain (as inert, isolated text), only the tricks go


def test_the_scope_rejects_a_desktop_field_it_does_not_define() -> None:
    with pytest.raises(ValueError):
        DesktopDiscloseScope(
            observation_id=uuid.uuid4(), snapshot_digest="a" * 64, observed_at=NOW, worker_generation=uuid.uuid4(),
            surface_ref="s1", surface_epoch=1, recipient="openai", model="gpt-test",
            display=DisplayTarget(application_label="a", window_title="b"), hwnd=5,  # type: ignore[call-arg]
        )


# ---- L2: the reserved task type cannot be minted by the generic route, and cannot hide the real card ------


@pytest.fixture
def desktop(engine: AsyncEngine, runtime_generation: RuntimeGeneration) -> FakeDesktop:
    return FakeDesktop(engine, runtime_generation.id)


@pytest.fixture
def service(engine: AsyncEngine, desktop: FakeDesktop) -> DesktopDisclosureService:
    return DesktopDisclosureService(engine, desktop=cast(DesktopService, desktop), grant_ttl_seconds=600)


async def test_the_generic_task_route_cannot_create_a_desktop_read(settings: Settings, engine: AsyncEngine) -> None:
    quiet = settings.model_copy(update={"public_inspection_hosts": "", "inspection_test_origins": ""})
    async with running_app(create_app(quiet)) as client:
        refused = await client.post("/tasks", json={"request": {"type": "desktop_read", "objective": "x"}})
        assert refused.status_code == 422
        assert isinstance(client, httpx.AsyncClient)
    async with engine.connect() as connection:
        assert await connection.scalar(text("SELECT count(*) FROM tasks")) == 0


async def test_a_task_that_merely_names_the_reserved_type_cannot_hide_the_real_card(
    engine: AsyncEngine, service: DesktopDisclosureService, desktop: FakeDesktop
) -> None:
    real = await service.create(
        objective="What is failing?", recipient="openai", model="gpt-test",
        worker_generation=desktop.worker_generation, surface_ref="s1", surface_epoch=1,
    )
    async with engine.begin() as connection:  # what a bug elsewhere could do: a newer task with the type but no grant
        await connection.execute(
            text(
                "INSERT INTO tasks (id, status, revision, last_event_sequence, request) "
                "VALUES (:id, 'CREATED', 1, 1, CAST(:request AS jsonb))"
            ),
            {"id": uuid.uuid4(), "request": json.dumps({"type": "desktop_read", "objective": "decoy"})},
        )
    latest = await service.latest()
    assert latest is not None and latest.task_id == real.task_id


# ---- M4 (service): a database refusal of the answer is a recorded failure, not a lost approval ----------


async def test_a_database_refusal_of_the_private_answer_is_recorded_as_a_failed_attempt(
    engine: AsyncEngine, service: DesktopDisclosureService, desktop: FakeDesktop, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqlalchemy.exc import DBAPIError

    from app.repositories.desktop_disclosure import DesktopDisclosureRepository

    view = await service.create(
        objective="What is failing?", recipient="openai", model="gpt-test",
        worker_generation=desktop.worker_generation, surface_ref="s1", surface_epoch=1,
    )
    assert view.card is not None
    await service.confirm(view.task_id, grant_id=view.card.grant_id, expected_revision=view.card.grant_revision)
    context = await service.claim(view.task_id)
    text_node = next(item for item in context.projection["nodes"] if item.get("text"))

    async def refuse(self: Any, **values: Any) -> None:
        raise DBAPIError("INSERT ... [parameters: secret answer text]", None, Exception("secret answer text"))

    monkeypatch.setattr(DesktopDisclosureRepository, "insert_answer", refuse)
    done = await service.record_result(
        context.task_id, disclosure_id=context.disclosure_id, failure=None,
        result={
            "schema_version": 1, "kind": "answer", "answer": f"It says {text_node['text']}.",
            "evidence": [{"control_ref": text_node["control_ref"], "quote": text_node["text"]}],
        },
    )
    assert done.phase == "failed" and done.disclosure is not None and done.disclosure.error_code == "invalid_output"
    async with engine.connect() as connection:
        assert await connection.scalar(text("SELECT count(*) FROM desktop_answers")) == 0
        status = await connection.scalar(text("SELECT status FROM desktop_disclosures"))
    assert status == "FAILED"
