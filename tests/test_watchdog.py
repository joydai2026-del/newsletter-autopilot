"""The watchdog: independent, honest about its own failure, timezone-correct."""

from __future__ import annotations

import importlib.util
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

# Loaded BY PATH, not imported as a package member. That is the point of the
# design: the watchdog must run when the pipeline is missing entirely, so the
# test proves it has no dependency on it.
_SPEC = importlib.util.spec_from_file_location(
    "outcome_check",
    Path(__file__).resolve().parents[1] / "watchdog" / "outcome_check.py")
outcome_check = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(outcome_check)                    # type: ignore[union-attr]

TZ = ZoneInfo("America/New_York")
WEEKDAYS = {0, 1, 2, 3, 4}


def test_watchdog_imports_nothing_from_the_pipeline():
    src = (Path(__file__).resolve().parents[1] / "watchdog" / "outcome_check.py").read_text()
    assert "newsletter_autopilot" not in src
    assert "from ." not in src


def _check(now: str, posts, monkeypatch, **kw):
    alarms: list[str] = []
    monkeypatch.setattr(outcome_check, "fetch_archive",
                        (lambda url, timeout=30: posts) if not isinstance(posts, Exception)
                        else _raiser(posts))
    monkeypatch.setattr(outcome_check, "alarm", lambda msg, hook: alarms.append(msg))
    code = outcome_check.check(
        archive_url="https://example.invalid/archive", tz=TZ, publish_hour=9,
        publish_minute=0, grace_minutes=60, publish_days=WEEKDAYS,
        date_field="post_date", webhook=None,
        now=datetime.fromisoformat(now).replace(tzinfo=TZ), **kw)
    return code, alarms


def _raiser(exc):
    def _f(url, timeout=30):
        raise exc
    return _f


def test_published_today_is_all_clear(monkeypatch):
    posts = [{"post_date": "2026-08-26T13:00:00Z", "title": "Today's issue"}]
    code, alarms = _check("2026-08-26T11:00", posts, monkeypatch)
    assert code == 0 and alarms == []


def test_silent_miss_alarms(monkeypatch):
    posts = [{"post_date": "2026-08-25T13:00:00Z", "title": "Yesterday"}]
    code, alarms = _check("2026-08-26T11:00", posts, monkeypatch)
    assert code == 1
    assert "NO issue published today" in alarms[0]


def test_a_broken_check_alarms_and_never_reads_as_all_clear(monkeypatch):
    code, alarms = _check("2026-08-26T11:00", RuntimeError("archive is html"), monkeypatch)
    assert code == 2
    assert "COULD NOT RUN" in alarms[0]


def test_quiet_before_the_slot_plus_grace(monkeypatch):
    code, alarms = _check("2026-08-26T09:30", [], monkeypatch)
    assert code == 0 and alarms == []


def test_quiet_on_a_non_publishing_day(monkeypatch):
    code, alarms = _check("2026-08-29T18:00", [], monkeypatch)   # a Saturday
    assert code == 0 and alarms == []


def test_an_evening_send_is_still_today_after_the_utc_conversion():
    """A 21:05 Eastern send is stored on the NEXT UTC day. Comparing raw UTC
    against a local date makes it invisible, which is how an evening recovery
    escapes the one check that catches a double send."""
    posts = [{"post_date": "2026-08-27T01:05:00Z", "title": "Evening recovery"}]
    now = datetime.fromisoformat("2026-08-26T22:00").replace(tzinfo=TZ)
    found, detail = outcome_check.published_today(posts, now, TZ, "post_date")
    assert found is True
    assert "Evening recovery" in detail


def test_an_unreadable_timestamp_does_not_count_as_a_match():
    posts = [{"post_date": "not a date", "title": "junk"}]
    now = datetime.fromisoformat("2026-08-26T11:00").replace(tzinfo=TZ)
    found, _ = outcome_check.published_today(posts, now, TZ, "post_date")
    assert found is False


def test_an_empty_archive_is_reported_as_such():
    now = datetime.fromisoformat("2026-08-26T11:00").replace(tzinfo=TZ)
    found, detail = outcome_check.published_today([], now, TZ, "post_date")
    assert found is False and "came back empty" in detail


@pytest.mark.parametrize("payload,expected", [
    ([{"post_date": "x"}], 1),
    ({"posts": [{"post_date": "x"}]}, 1),
    ({"items": [{"post_date": "x"}, {"post_date": "y"}]}, 2),
])
def test_archive_shapes(payload, expected, monkeypatch):
    class _Resp:
        status = 200

        def read(self):
            import json
            return json.dumps(payload).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(outcome_check.urllib.request, "urlopen", lambda *a, **k: _Resp())
    assert len(outcome_check.fetch_archive("https://example.invalid")) == expected


def test_a_non_list_archive_raises(monkeypatch):
    class _Resp:
        status = 200

        def read(self):
            return b'"a string, not a list"'

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(outcome_check.urllib.request, "urlopen", lambda *a, **k: _Resp())
    with pytest.raises(RuntimeError):
        outcome_check.fetch_archive("https://example.invalid")


def test_cli_wiring(monkeypatch, capsys):
    monkeypatch.setattr(outcome_check, "fetch_archive",
                        lambda url, timeout=30: [{"post_date": "2026-08-26T13:00:00Z",
                                                  "title": "ok"}])
    code = outcome_check.main(["--archive-url", "https://example.invalid",
                               "--now", "2026-08-26T11:00-04:00"])
    assert code == 0
    assert "published today" in capsys.readouterr().out


assert sys.modules  # keep the import used


def test_an_unrelated_post_today_does_not_mask_a_missed_issue():
    """Without --title-contains, any post today reads as all clear, so a one-off
    announcement can hide a newsletter that never went out."""
    posts = [{"post_date": "2026-08-26T15:00:00Z", "title": "A note to readers"}]
    now = datetime.fromisoformat("2026-08-26T18:00").replace(tzinfo=TZ)
    assert outcome_check.published_today(posts, now, TZ, "post_date")[0] is True
    found, _ = outcome_check.published_today(posts, now, TZ, "post_date",
                                             title_contains="Daily")
    assert found is False


def test_a_naive_now_is_read_in_the_configured_zone_not_the_machines(monkeypatch):
    """The same command must answer the same on a laptop and on a server."""
    seen = {}
    monkeypatch.setattr(outcome_check, "fetch_archive", lambda url, timeout=30: [])
    monkeypatch.setattr(outcome_check, "alarm", lambda m, h: seen.setdefault("m", m))
    # 09:30 in America/New_York is before the slot plus grace, so this is quiet.
    assert outcome_check.main(["--archive-url", "https://example.invalid",
                               "--now", "2026-08-26T09:30"]) == 0
    assert "m" not in seen
