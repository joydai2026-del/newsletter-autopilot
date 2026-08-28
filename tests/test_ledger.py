"""The dedup ledger: idempotent by date, atomic, and never empty."""

from __future__ import annotations

import pytest

from newsletter_autopilot.errors import LedgerError
from newsletter_autopilot.ledger import Ledger, filter_unused, record_issue


def test_records_and_reads_back(tmp_path):
    led = Ledger("images", tmp_path)
    assert led.record("2026-08-26", ["a.png", "b.png"]) is True
    assert led.recorded("2026-08-26") is True
    assert led.entries_for("2026-08-26") == ["a.png", "b.png"]
    assert led.all_entries() == {"a.png", "b.png"}


def test_second_record_for_the_same_date_is_a_no_op(tmp_path):
    """A resumed or re-run publish must not double-write."""
    led = Ledger("images", tmp_path)
    led.record("2026-08-26", ["a.png"])
    assert led.record("2026-08-26", ["c.png"]) is False
    assert led.entries_for("2026-08-26") == ["a.png"]


def test_refuses_an_empty_list(tmp_path):
    """An empty record would set the guard for that date and make the real
    entries unrecordable forever."""
    led = Ledger("images", tmp_path)
    with pytest.raises(LedgerError):
        led.record("2026-08-26", [])
    with pytest.raises(LedgerError):
        led.record("2026-08-26", ["", "   "])
    assert not led.recorded("2026-08-26")


def test_rejects_a_bad_date_and_an_unsafe_name(tmp_path):
    with pytest.raises(LedgerError):
        Ledger("images", tmp_path).record("26 Aug", ["a.png"])
    with pytest.raises(LedgerError):
        Ledger("../escape", tmp_path)


def test_separate_dates_stay_separate(tmp_path):
    led = Ledger("images", tmp_path)
    led.record("2026-08-25", ["a.png"])
    led.record("2026-08-26", ["b.png"])
    assert led.entries_for("2026-08-25") == ["a.png"]
    assert led.entries_for("2026-08-26") == ["b.png"]
    assert led.all_entries() == {"a.png", "b.png"}


def test_filter_unused_drops_repeats_and_dupes_within_the_batch(tmp_path):
    Ledger("images", tmp_path).record("2026-08-25", ["seen.png"])
    got = filter_unused(tmp_path, "images", ["seen.png", "new.png", "new.png", "x.png"])
    assert got == ["new.png", "x.png"]


def test_record_issue_attempts_every_ledger_even_when_one_fails(tmp_path):
    """One ledger's failure must not cost the others their entry."""
    with pytest.raises(LedgerError) as exc:
        record_issue(tmp_path, "2026-08-26", {"images": ["a.png"], "segments": []})
    assert "segments" in str(exc.value)
    # The good one still landed.
    assert Ledger("images", tmp_path).entries_for("2026-08-26") == ["a.png"]


def test_file_is_human_readable_markdown(tmp_path):
    led = Ledger("images", tmp_path)
    led.record("2026-08-25", ["a.png"])
    led.record("2026-08-26", ["b.png"])
    text = led.path.read_text("utf8")
    assert text.startswith("## 2026-08-25\n- a.png\n")
    assert "\n\n## 2026-08-26\n- b.png\n" in text


def test_no_temp_files_are_left_behind(tmp_path):
    led = Ledger("images", tmp_path)
    led.record("2026-08-26", ["a.png"])
    assert [p.name for p in tmp_path.iterdir()] == ["images.md"]
