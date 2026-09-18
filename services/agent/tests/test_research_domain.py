"""The research vocabulary, refs, scope and grounding, without a database.

These are the tests that pin the *shape* of Milestone 7b: what a planner is
able to say at all, what a scope means, and what an answer has to prove.
"""

import uuid
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.domain.public_url import RESEARCH_POLICY_VERSION, PublicUrlPolicy, UrlPolicyError
from app.domain.research import (
    ALLOWED_SUMMARY,
    BROWSER_OPERATIONS,
    FORBIDDEN_SUMMARY,
    RESEARCH_TOOL_NAMES,
    TOOL_NAMES,
    AnswerNotGroundedError,
    ObservedLink,
    ResearchAnswer,
    ResearchBudgets,
    ResearchDisclosure,
    ResearchEvidence,
    ResearchObservation,
    ResearchOperation,
    ResearchProposal,
    ResearchRefusal,
    ResearchScope,
    SearchResult,
    TextBlock,
    compute_content_hash,
    extract_seeds,
    parse_objective,
    parse_search_query,
    parse_step,
    verify_research_grounding,
)
from app.services.research_search import SearchConfig, parse_results

# ---- the step vocabulary ---------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        {"operation": "public_search", "query": "lumi repository"},
        {"operation": "navigate", "tab": "t1", "target": {"kind": "seed", "ref": "s1"}},
        {"operation": "navigate", "tab": "t2", "target": {"kind": "link", "observation": "o3", "ref": "l5"}},
        {"operation": "navigate", "tab": "t1", "target": {"kind": "result", "observation": "o1", "ref": "r2"}},
        {"operation": "observe", "tab": "t1"},
        {"operation": "scroll", "tab": "t1", "direction": "down"},
        {"operation": "history", "tab": "t1", "direction": "back"},
        {"operation": "tab", "action": "open"},
        {"operation": "tab", "action": "close", "tab": "t3"},
    ],
)
def test_the_reviewed_operations_parse(payload: dict[str, object]) -> None:
    step = parse_step(payload)
    assert step.operation in {operation.value for operation in ResearchOperation}


@pytest.mark.parametrize(
    "payload",
    [
        # Nothing in this list is a typo. Each one is a capability a planner
        # might try to reach for, and none of them has a field to reach with.
        {"operation": "navigate", "tab": "t1", "url": "https://example.com/"},
        {"operation": "navigate", "tab": "t1", "target": {"kind": "url", "ref": "https://x.test/"}},
        {"operation": "click", "selector": "div:nth-child(4) a"},
        {"operation": "observe", "tab": "t1", "selector": ".stats"},
        {"operation": "observe", "tab": "t1", "xpath": "//a[1]"},
        {"operation": "evaluate", "script": "fetch('/x')"},
        {"operation": "observe", "tab": "t1", "script": "window.x"},
        {"operation": "navigate", "tab": "t1", "target": {"kind": "seed", "ref": "s1"}, "method": "POST"},
        {"operation": "navigate", "tab": "t1", "target": {"kind": "seed", "ref": "s1"}, "headers": {"a": "b"}},
        {"operation": "navigate", "tab": "t1", "target": {"kind": "seed", "ref": "s1"}, "cookies": []},
        {"operation": "scroll", "tab": "t1", "direction": "down", "x": 10, "y": 20},
        {"operation": "type", "tab": "t1", "text": "password"},
        {"operation": "submit", "tab": "t1"},
        {"operation": "download", "tab": "t1"},
        {"operation": "login", "tab": "t1"},
        # Refs that are not refs.
        {"operation": "navigate", "tab": "t9", "target": {"kind": "seed", "ref": "s1"}},
        {"operation": "navigate", "tab": "t1", "target": {"kind": "link", "observation": "o0", "ref": "l1"}},
        {"operation": "navigate", "tab": "t1", "target": {"kind": "link", "observation": "o1", "ref": "link_07"}},
        {"operation": "navigate", "tab": "t1", "target": {"kind": "seed", "ref": "s4"}},
        # A tab action whose ref does not match what the action needs.
        {"operation": "tab", "action": "open", "tab": "t1"},
        {"operation": "tab", "action": "close"},
    ],
)
def test_anything_outside_the_vocabulary_is_refused(payload: dict[str, object]) -> None:
    with pytest.raises(ResearchRefusal) as refusal:
        parse_step(payload)
    assert refusal.value.code == "unsupported_operation"


def test_every_operation_has_one_ledger_tool_name() -> None:
    assert set(TOOL_NAMES) == set(ResearchOperation)
    assert len(set(TOOL_NAMES.values())) == len(ResearchOperation)
    assert RESEARCH_TOOL_NAMES == frozenset(TOOL_NAMES.values())
    # Search is the runtime's own bounded JSON GET, not browser work: no search
    # credential and no search-result markup goes near the browser.
    assert ResearchOperation.SEARCH not in BROWSER_OPERATIONS


# ---- what a planner may put in a search query ------------------------------------


def test_a_plain_query_is_accepted() -> None:
    assert parse_search_query("  lumi   desktop   repository ") == "lumi desktop repository"


@pytest.mark.parametrize(
    ("query", "code"),
    [
        ("see https://internal.example/x", "query_contains_url"),
        ("contact satish@example.com", "query_contains_address"),
        ("account 40012345678", "query_contains_identifier"),
        ("lumi api_key", "query_contains_secret"),
        ("my password for github", "query_contains_secret"),
        ("", "query_invalid"),
        ("x" * 300, "query_invalid"),
    ],
)
def test_a_query_that_could_carry_private_data_out_is_refused(query: str, code: str) -> None:
    """"The user asked for research" cannot authorise appending anything."""
    with pytest.raises(ResearchRefusal) as refusal:
        parse_search_query(query)
    assert refusal.value.code == code


def test_an_objective_must_be_plain_bounded_text() -> None:
    assert parse_objective("  Find the  Lumi repository ") == "Find the Lumi repository"
    for value in ("", "x" * 600, "bad\u0000text", 7):
        with pytest.raises(ResearchRefusal):
            parse_objective(value)


# ---- seeds -----------------------------------------------------------------------


def test_only_addresses_the_user_typed_become_seeds() -> None:
    seeds = extract_seeds(
        "Look at https://github.com/satish9177/Lumi and https://example.com/a, then summarise"
    )
    assert seeds == ["https://github.com/satish9177/Lumi", "https://example.com/a"]
    assert extract_seeds("find the lumi repository") == []


def test_seeds_are_bounded_and_deduplicated() -> None:
    objective = " ".join(f"https://host{index}.example.com/" for index in range(10))
    assert len(extract_seeds(objective)) == 3
    assert extract_seeds("https://a.example.com/ https://a.example.com/") == ["https://a.example.com/"]


# ---- the scope -------------------------------------------------------------------


def _scope(**overrides: object) -> ResearchScope:
    base: dict[str, object] = {
        "policy_version": RESEARCH_POLICY_VERSION,
        "allowed_operations": [
            ResearchOperation.SEARCH,
            ResearchOperation.NAVIGATE,
            ResearchOperation.OBSERVE,
        ],
        "disclosure": ResearchDisclosure(recipients=["scripted"], max_text_chars=10_000),
    }
    return ResearchScope(**{**base, **overrides})


def test_a_scope_permits_only_what_it_lists() -> None:
    scope = _scope()
    assert scope.permits(ResearchOperation.SEARCH)
    assert scope.permits(ResearchOperation.NAVIGATE)
    assert not scope.permits(ResearchOperation.TAB)
    assert not scope.permits(ResearchOperation.SCROLL)


def test_the_card_promises_the_same_words_the_runtime_enforces() -> None:
    scope = _scope()
    assert list(scope.allowed) == list(ALLOWED_SUMMARY)
    assert list(scope.forbidden) == list(FORBIDDEN_SUMMARY)
    for forbidden in ("login", "forms_and_typing", "uploads_and_downloads", "non_get_requests"):
        assert forbidden in scope.forbidden
    assert scope.methods == ["GET", "HEAD"]


def test_the_scope_digest_changes_with_the_scope() -> None:
    narrow = _scope()
    wide = _scope(allowed_operations=[*narrow.allowed_operations, ResearchOperation.TAB])
    assert narrow.digest != wide.digest
    assert narrow.digest == _scope().digest


def test_a_scope_refuses_unknown_fields_and_duplicate_operations() -> None:
    with pytest.raises(ValidationError):
        _scope(login=True)
    with pytest.raises(ValidationError):
        _scope(allowed_operations=[ResearchOperation.OBSERVE, ResearchOperation.OBSERVE])


def test_seed_refs_resolve_only_inside_their_own_scope() -> None:
    scope = _scope(seeds=["https://example.com/a"])
    assert scope.seed("s1") == "https://example.com/a"
    assert scope.seed("s2") is None
    assert _scope().seed("s1") is None


def test_default_budgets_are_the_reviewed_starting_values() -> None:
    budgets = ResearchBudgets()
    assert (budgets.max_steps, budgets.max_observations, budgets.max_planner_calls) == (20, 30, 20)
    assert (budgets.max_tabs, budgets.max_active_seconds) == (5, 300)
    assert (budgets.max_model_input_tokens, budgets.max_model_output_tokens) == (60_000, 8_000)
    assert budgets.max_vision_calls == 2


# ---- observations ----------------------------------------------------------------


def _page_observation(**overrides: object) -> ResearchObservation:
    blocks = [TextBlock(id="b1", text="Contributors 7"), TextBlock(id="b2", text="Stars 1,204")]
    links = [ObservedLink(id="l1", text="Project", host="example.com")]
    base: dict[str, object] = {
        "observation_id": uuid.uuid4(),
        "kind": "page",
        "operation": ResearchOperation.NAVIGATE,
        "sequence": 2,
        "session_id": uuid.uuid4(),
        "tab": "t1",
        "document_epoch": 3,
        "final_url": "https://example.com/project",
        "final_host": "example.com",
        "title": "Project",
        "observed_at": datetime.now(UTC),
        "blocks": blocks,
        "links": links,
        "content_hash": compute_content_hash(
            kind="page",
            final_url="https://example.com/project",
            title="Project",
            blocks=blocks,
            links=links,
            results=[],
        ),
    }
    return ResearchObservation(**{**base, **overrides})


def test_an_observation_is_untrusted_and_self_describing() -> None:
    observation = _page_observation()
    assert observation.provenance == "untrusted_environment"
    assert observation.ref == "o2"
    assert observation.block("b1") is not None
    assert observation.block("b9") is None


def test_an_observation_whose_hash_does_not_match_its_content_is_refused() -> None:
    with pytest.raises(ValidationError):
        _page_observation(content_hash="0" * 64)


def test_an_observed_link_carries_a_ref_a_label_and_a_host_but_no_address() -> None:
    link = ObservedLink(id="l1", text="Project", host="example.com")
    assert set(link.model_dump()) == {"id", "text", "host"}
    with pytest.raises(ValidationError):
        ObservedLink(  # type: ignore[call-arg]
            id="l1", text="Project", host="example.com", url="https://example.com/"
        )


def test_refs_must_be_sequential_so_a_page_cannot_choose_its_own_identifiers() -> None:
    with pytest.raises(ValidationError):
        _page_observation(blocks=[TextBlock(id="b2", text="skipped b1")])


def test_a_search_observation_carries_results_and_no_page_text() -> None:
    results = [SearchResult(id="r1", title="Hub", host="example.com", snippet="An index")]
    observation = ResearchObservation(
        observation_id=uuid.uuid4(),
        kind="search_results",
        operation=ResearchOperation.SEARCH,
        sequence=1,
        query="lumi",
        observed_at=datetime.now(UTC),
        results=results,
        content_hash=compute_content_hash(
            kind="search_results", final_url="", title="", blocks=[], links=[], results=results
        ),
    )
    assert observation.results[0].id == "r1"
    assert observation.blocks == []


# ---- the immutable step proposal -------------------------------------------------


def test_a_proposal_records_the_resolved_destination_a_model_never_supplied() -> None:
    proposal = ResearchProposal(
        operation=ResearchOperation.NAVIGATE,
        step_number=3,
        policy_version=RESEARCH_POLICY_VERSION,
        grant_id=uuid.uuid4(),
        scope_digest="a" * 64,
        step={"operation": "navigate", "tab": "t1", "target": {"kind": "link", "observation": "o2", "ref": "l1"}},
        destination_url="https://example.com/project",
        destination_host="example.com",
        session_id=uuid.uuid4(),
        tab="t1",
        expected_document_epoch=3,
    )
    assert proposal.kind == "public_research_step"
    assert proposal.destination_url == "https://example.com/project"


def test_a_proposal_will_not_hold_a_step_outside_the_vocabulary() -> None:
    with pytest.raises(ValidationError):
        ResearchProposal(
            operation=ResearchOperation.NAVIGATE,
            step_number=1,
            policy_version=RESEARCH_POLICY_VERSION,
            grant_id=uuid.uuid4(),
            scope_digest="a" * 64,
            step={"operation": "click", "selector": "a"},
        )


# ---- grounding -------------------------------------------------------------------


def _answer(**overrides: object) -> ResearchAnswer:
    base: dict[str, object] = {
        "status": "answered",
        "stop_reason": "goal_reached",
        "answer": "The project has 7 contributors.",
        "evidence": [ResearchEvidence(observation="o2", block="b1", quote="Contributors 7")],
    }
    return ResearchAnswer(**{**base, **overrides})


def test_a_grounded_answer_is_accepted() -> None:
    observation = _page_observation()
    verify_research_grounding({observation.ref: observation}, _answer())


@pytest.mark.parametrize(
    ("answer", "code"),
    [
        (_answer(evidence=[]), "no_evidence"),
        (
            _answer(evidence=[ResearchEvidence(observation="o9", block="b1", quote="Contributors 7")]),
            "unknown_observation",
        ),
        (
            _answer(evidence=[ResearchEvidence(observation="o2", block="b9", quote="Contributors 7")]),
            "unknown_block",
        ),
        (
            _answer(evidence=[ResearchEvidence(observation="o2", block="b1", quote="Contributors 42")]),
            "quote_not_in_block",
        ),
        (
            _answer(answer="The project has 999 contributors."),
            "number_not_in_evidence",
        ),
    ],
)
def test_an_answer_the_observations_do_not_support_is_refused(
    answer: ResearchAnswer, code: str
) -> None:
    observation = _page_observation()
    with pytest.raises(AnswerNotGroundedError) as refusal:
        verify_research_grounding({observation.ref: observation}, answer)
    assert refusal.value.code == code


def test_a_number_from_an_uncited_observation_is_not_evidence() -> None:
    """The distractor case: the right label on the wrong page proves nothing."""
    correct = _page_observation()
    decoy_blocks = [TextBlock(id="b1", text="Contributors 41")]
    decoy = ResearchObservation(
        observation_id=uuid.uuid4(),
        kind="page",
        operation=ResearchOperation.NAVIGATE,
        sequence=3,
        final_url="https://example.com/lamp",
        final_host="example.com",
        title="Lamp",
        observed_at=datetime.now(UTC),
        blocks=decoy_blocks,
        content_hash=compute_content_hash(
            kind="page",
            final_url="https://example.com/lamp",
            title="Lamp",
            blocks=decoy_blocks,
            links=[],
            results=[],
        ),
    )
    observations = {correct.ref: correct, decoy.ref: decoy}
    with pytest.raises(AnswerNotGroundedError) as refusal:
        verify_research_grounding(observations, _answer(answer="It has 41 contributors."))
    assert refusal.value.code == "number_not_in_evidence"


def test_not_found_needs_no_evidence_and_may_not_smuggle_numbers() -> None:
    observation = _page_observation()
    verify_research_grounding(
        {observation.ref: observation},
        ResearchAnswer(
            status="not_found",
            stop_reason="no_evidence",
            answer="Lumi could not verify that from the public pages it read.",
        ),
    )


# ---- the research destination policy ----------------------------------------------


def _research_policy(**overrides: object) -> PublicUrlPolicy:
    return PublicUrlPolicy(
        version=RESEARCH_POLICY_VERSION, allow_any_public_host=True, **overrides  # type: ignore[arg-type]
    )


@pytest.mark.parametrize(
    ("url", "code"),
    [
        ("http://example.com/", "https_required"),
        ("https://localhost/", "local_host"),
        ("https://router.local/", "local_host"),
        ("https://intranet.corp/", "local_host"),
        ("https://127.0.0.1/", "ip_literal"),
        ("https://127.1/", "ip_literal"),
        ("https://0x7f000001/", "local_host"),
        ("https://2130706433/", "local_host"),
        ("https://[::1]/", "invalid_host"),
        ("https://169.254.169.254/latest/meta-data/", "ip_literal"),
        ("https://user:pass@example.com/", "credentials_in_url"),
        ("https://example.com:8443/", "port_not_allowed"),
        ("file:///C:/Windows/win.ini", "scheme_not_allowed"),
        ("data:text/html,<h1>x", "invalid_url"),
        ("javascript:alert(1)", "scheme_not_allowed"),
        ("about:blank", "scheme_not_allowed"),
        ("chrome://settings", "scheme_not_allowed"),
        ("devtools://devtools/bundled/x.html", "scheme_not_allowed"),
        ("ws://example.com/socket", "scheme_not_allowed"),
        ("wss://example.com/socket", "scheme_not_allowed"),
        ("lumi://open", "scheme_not_allowed"),
        ("https://example.com/\\..\\x", "invalid_url"),
        ("https://exa mple.com/", "invalid_url"),
    ],
)
def test_research_may_reach_any_public_host_but_no_forbidden_destination(
    url: str, code: str
) -> None:
    """Allowing any *public* host does not allow anything else."""
    with pytest.raises(UrlPolicyError) as refusal:
        _research_policy().check(url)
    assert refusal.value.code == code


def test_research_accepts_an_ordinary_public_address_the_m7a_policy_would_refuse() -> None:
    assert _research_policy().check("https://github.com/a/b?c=d#frag").url == (
        "https://github.com/a/b?c=d"
    )
    # The Milestone 7a policy is untouched: no allowlist entry, no inspection.
    with pytest.raises(UrlPolicyError) as refusal:
        PublicUrlPolicy().check("https://github.com/a/b")
    assert refusal.value.code == "destination_not_allowed"


def test_a_narrowed_research_policy_still_uses_its_allowlist() -> None:
    policy = PublicUrlPolicy(
        allowed_hosts=frozenset({"github.com"}), version=RESEARCH_POLICY_VERSION
    )
    assert policy.check("https://github.com/x").host == "github.com"
    with pytest.raises(UrlPolicyError) as refusal:
        policy.check("https://elsewhere.example.com/x")
    assert refusal.value.code == "destination_not_allowed"


# ---- the search reader -------------------------------------------------------------


def test_the_search_reader_drops_results_the_policy_refuses() -> None:
    policy = _research_policy()
    results = parse_results(
        {
            "results": [
                {"title": "Good", "url": "https://example.com/a", "snippet": "ok"},
                {"title": "Loopback", "url": "http://127.0.0.1:9/x", "snippet": "no"},
                {"title": "Metadata", "url": "https://169.254.169.254/", "snippet": "no"},
                {"title": "File", "url": "file:///C:/x", "snippet": "no"},
                {"title": "Duplicate", "url": "https://example.com/a", "snippet": "dupe"},
            ]
        },
        policy,
    )
    assert [result.url for result in results] == ["https://example.com/a"]


@pytest.mark.parametrize(
    "body",
    [
        {"results": [{"title": "A", "url": "https://example.com/a"}]},
        {"web": {"results": [{"title": "A", "url": "https://example.com/a"}]}},
        {"webPages": {"value": [{"name": "A", "url": "https://example.com/a"}]}},
        {"organic_results": [{"title": "A", "link": "https://example.com/a"}]},
    ],
)
def test_the_search_reader_understands_the_documented_shapes(body: dict[str, object]) -> None:
    results = parse_results(body, _research_policy())
    assert [result.url for result in results] == ["https://example.com/a"]


def test_search_is_absent_until_an_endpoint_is_configured() -> None:
    assert not SearchConfig().configured
    assert not SearchConfig(endpoint="https://search.example.com/?q=fixed").configured
    assert SearchConfig(endpoint="https://search.example.com/?q={query}").configured
