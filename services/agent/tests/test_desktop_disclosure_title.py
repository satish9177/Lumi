"""Milestone 9 S2: the window title is display text for the trusted card and is not given to a provider.

Found by the real-window test: UI Automation names the top-level window node with the window's own title
(and a title-bar control usually repeats it), so a naive projection of the tree would send the title to the
provider even though the card promises it does not. Titles routinely carry file names and customer names.
"""

import json
import uuid
from datetime import UTC, datetime

from app.desktop.protocol import DesktopRole
from app.domain.desktop_disclosure import DesktopDiscloseScope, DisplayTarget, build_projection
from tests.desktop_disclosure_support import VISIBLE, node, observation

TITLE = "Quarterly-Report-ACME-Customers.xlsx - Editor"
NOW = datetime(2026, 9, 21, 10, 0, tzinfo=UTC)


def snapshot() -> dict[str, object]:
    nodes = [
        node(1, name=TITLE, role=DesktopRole.WINDOW),
        node(2, name=TITLE, role=DesktopRole.TITLE_BAR, parent=1),
        node(3, name="Status", text=VISIBLE, parent=1),
        node(4, name="Editor", parent=1),
        node(5, text=f"Saved {TITLE.upper()}", parent=1),
    ]
    return observation(nodes, worker_generation=uuid.uuid4()).model_dump(mode="json")


def scope_with(title: str, label: str) -> DesktopDiscloseScope:
    return DesktopDiscloseScope(
        observation_id=uuid.uuid4(), snapshot_digest="a" * 64, observed_at=NOW, worker_generation=uuid.uuid4(),
        surface_ref="s1", surface_epoch=1, recipient="scripted", model="scripted-rules",
        display=DisplayTarget(application_label=label, window_title=title),
    )


def test_the_top_level_window_node_keeps_its_role_but_never_its_name() -> None:
    projection = build_projection(snapshot(), observed_at=NOW)
    root = projection.node("u1")
    assert root is not None and root["role"] == "window" and "name" not in root


def test_a_node_that_is_exactly_the_window_title_or_label_is_emptied() -> None:
    scope = scope_with(TITLE, "Editor")
    projection = build_projection(snapshot(), observed_at=NOW, withhold=scope.withheld())
    dumped = json.dumps(projection.payload, ensure_ascii=False)
    assert TITLE not in dumped
    assert VISIBLE in dumped
    title_bar = projection.node("u2")
    assert title_bar is not None and "name" not in title_bar
    label_node = projection.node("u4")
    assert label_node is not None and "name" not in label_node


def test_a_title_quoted_inside_other_text_is_a_documented_residual() -> None:
    scope = scope_with(TITLE, "Editor")
    projection = build_projection(snapshot(), observed_at=NOW, withhold=scope.withheld())
    shouting = projection.node("u5")
    assert shouting is not None and TITLE.upper() in shouting["text"], "a title quoted inside other text is a documented residual"


def test_withholding_is_part_of_the_projection_digest_so_it_cannot_be_skipped_silently() -> None:
    plain = build_projection(snapshot(), observed_at=NOW)
    withheld = build_projection(snapshot(), observed_at=NOW, withhold=scope_with(TITLE, "Editor").withheld())
    assert plain.digest != withheld.digest
    assert build_projection(snapshot(), observed_at=NOW, withhold=scope_with(TITLE, "Editor").withheld()).digest == withheld.digest
