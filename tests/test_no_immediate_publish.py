"""The claim: no adapter exposes an immediate-publish call.

This deserves a real test rather than one `hasattr` check, because it is the
property that removes "just send it now" as a reachable state, for a human and
for an agent. A single negative assertion on one guessed name would pass on an
adapter with a differently named send method, which is exactly the bug it is
supposed to prevent.
"""

from __future__ import annotations

import re
from pathlib import Path

from newsletter_autopilot.adapters import base
from newsletter_autopilot.adapters.base import PublisherAdapter
from newsletter_autopilot.adapters.mock import MockPublisher

ADAPTER_DIR = Path(base.__file__).parent

# Anything that would send now rather than at a set trigger time.
PUBLISH_SHAPED = re.compile(
    r"\b(publish_now|send_now|publish_immediately|send_immediately|"
    r"post_now|deliver_now|blast|send_to_list|publish_draft)\b")


def protocol_members() -> set[str]:
    """Every name the adapter contract declares: methods plus annotations."""
    return ({n for n in vars(PublisherAdapter) if not n.startswith("_")}
            | set(getattr(PublisherAdapter, "__annotations__", {})))


def test_the_contract_has_no_immediate_publish_member():
    """Enumerate the protocol rather than guessing one name."""
    members = protocol_members()
    assert "schedule" in members, "sanity: the protocol was read correctly"
    assert not [m for m in members if PUBLISH_SHAPED.search(m)]
    # The only mutation that can make a post go out is the scheduled one.
    assert {"schedule", "unschedule"} <= members


def test_no_adapter_source_defines_a_publish_shaped_method():
    for path in sorted(ADAPTER_DIR.glob("*.py")):
        for n, line in enumerate(path.read_text("utf8").splitlines(), start=1):
            if line.lstrip().startswith("def ") and PUBLISH_SHAPED.search(line):
                raise AssertionError(f"{path.name}:{n} defines {line.strip()}")


def test_the_mock_conforms_to_the_contract(tmp_path):
    assert isinstance(MockPublisher(tmp_path), PublisherAdapter)


def test_the_platform_firing_helper_is_not_part_of_the_contract(tmp_path):
    """The mock CAN make a post public, standing in for the platform's own
    scheduler. It must not be reachable as an adapter capability."""
    assert "_simulate_platform_firing" not in protocol_members()
    assert hasattr(MockPublisher(tmp_path), "_simulate_platform_firing")


def test_the_pipeline_never_calls_the_firing_helper():
    src = Path(base.__file__).parents[1]
    for path in sorted(src.rglob("*.py")):
        if path.parent.name == "adapters":
            continue
        assert "_simulate_platform_firing" not in path.read_text("utf8"), path
