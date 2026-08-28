"""Local validation and the visual render gate. Nothing here contacts anything."""

from __future__ import annotations

import json

import pytest
from conftest import ISSUE_DATE, make_pipe

from newsletter_autopilot.config import Config, Policy
from newsletter_autopilot.cover import (
    Raster,
    RenderGateFailed,
    assert_render_ok,
    encode_png,
    png_dimensions,
    render_cover,
)
from newsletter_autopilot.issue import DraftRejected, build_issue, load_stories, select_stories

# --- ingest and draft ---------------------------------------------------------

def test_only_the_configured_number_of_stories_is_taken(stories, cfg):
    picked = select_stories(load_stories(stories), cfg)
    assert len(picked) == cfg.policy.stories_per_issue


def test_a_thin_queue_refuses_rather_than_shipping_short(stories, cfg, tmp_path):
    raw = json.loads(stories.read_text("utf8"))
    raw["stories"] = raw["stories"][:2]
    thin = tmp_path / "thin.json"
    thin.write_text(json.dumps(raw), encoding="utf8")
    with pytest.raises(DraftRejected):
        select_stories(load_stories(thin), cfg)


def test_a_story_missing_a_required_field_is_rejected(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text(json.dumps([{"id": "x", "headline": "no url"}]), encoding="utf8")
    with pytest.raises(DraftRejected):
        load_stories(p)


def test_em_dashes_are_refused_when_the_policy_forbids_them(stories, cfg):
    picked = select_stories(load_stories(stories), cfg)
    picked[0] = type(picked[0])(**{**picked[0].__dict__,
                                   "summary": "a dash — right here"})
    with pytest.raises(DraftRejected) as exc:
        build_issue(ISSUE_DATE, picked, cfg)
    assert "em dash" in str(exc.value)


def test_the_same_url_twice_is_only_used_once(stories, cfg, tmp_path):
    raw = json.loads(stories.read_text("utf8"))
    raw["stories"].insert(1, dict(raw["stories"][0], id="dupe"))
    p = tmp_path / "dupe.json"
    p.write_text(json.dumps(raw), encoding="utf8")
    picked = select_stories(load_stories(p), cfg)
    assert len({s.url for s in picked}) == len(picked)


def test_a_bad_input_costs_zero_remote_calls(cfg, adapter, tmp_path):
    p = tmp_path / "thin.json"
    p.write_text(json.dumps({"stories": []}), encoding="utf8")
    res = make_pipe(adapter, cfg).publish(ISSUE_DATE, p)
    assert not res.ok
    assert adapter.calls == []


def test_a_non_publishing_day_is_refused(cfg, adapter, stories):
    res = make_pipe(adapter, cfg).publish("2026-08-29", stories)      # a Saturday
    assert not res.ok and "not a publishing day" in res.detail


# --- cover and the render gate ------------------------------------------------

def test_a_rendered_cover_passes_the_gate_and_has_the_right_dimensions():
    png = render_cover(ISSUE_DATE, "Test Daily")
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    assert png_dimensions(png) == (1200, 630)


def test_a_blank_render_is_caught_even_at_correct_dimensions():
    """Exit code 0, a file on disk, correct dimensions, nothing in the middle."""
    blank = Raster(1200, 630, (18, 20, 26))
    with pytest.raises(RenderGateFailed) as exc:
        assert_render_ok(blank, encode_png(blank), expected=(1200, 630))
    assert "distinct colours" in str(exc.value)


def test_a_decorated_border_with_an_empty_middle_is_caught():
    """The interior sample is why this fails: a full-frame check would pass it."""
    r = Raster(400, 400, (10, 10, 10))
    r.rect(0, 0, 400, 6, (200, 30, 30))
    r.rect(0, 394, 400, 6, (30, 200, 30))
    with pytest.raises(RenderGateFailed) as exc:
        assert_render_ok(r, encode_png(r), expected=(400, 400))
    assert "interior" in str(exc.value)


def test_the_gate_reads_dimensions_out_of_the_encoded_bytes():
    r = Raster(400, 400, (10, 10, 10))
    r.text(20, 20, "HELLO", (255, 255, 255), scale=6)
    with pytest.raises(RenderGateFailed) as exc:
        assert_render_ok(r, encode_png(r), expected=(1200, 630))
    assert "encoded file says 400x400" in str(exc.value)


def test_non_png_bytes_are_rejected():
    with pytest.raises(RenderGateFailed):
        png_dimensions(b"not a png at all")


def test_covers_differ_between_dates():
    assert render_cover("2026-08-26", "Test") != render_cover("2026-08-27", "Test")


# --- config ------------------------------------------------------------------

def test_policy_is_data_and_can_be_changed_without_touching_a_stage():
    cfg = Config.from_dict({"publication_name": "Weekly Thing",
                            "policy": {"publish_at": "17:30",
                                       "publish_days": ["tue"],
                                       "stories_per_issue": 3,
                                       "images_per_issue": 3}})
    assert cfg.policy.publish_days == (1,)
    assert cfg.policy.slot_on("2026-08-25").hour == 17
    assert cfg.policy.is_publishing_day("2026-08-25") is True
    assert cfg.policy.is_publishing_day("2026-08-26") is False


def test_an_unknown_policy_key_is_refused_loudly():
    from newsletter_autopilot.errors import ConfigError
    with pytest.raises(ConfigError):
        Config.from_dict({"policy": {"publsh_at": "09:00"}})


def test_the_shipped_example_policy_loads():
    from pathlib import Path
    cfg = Config.load(Path(__file__).resolve().parents[1] / "examples" / "policy.toml")
    assert cfg.policy.publish_days == (0, 1, 2, 3, 4)
    assert cfg.policy.stories_per_issue == 5


def test_defaults_are_a_working_policy():
    assert Policy().publish_at == "09:00"
    assert Policy().slot_on("2026-08-26").tzinfo is not None


def test_the_gate_measures_the_encoded_bytes_not_the_buffer():
    """A perfect in-memory raster with a blank ENCODING must still fail. This is
    the failure a buffer-only gate cannot see."""
    from newsletter_autopilot.cover import decode_png

    good = Raster(400, 400, (10, 10, 10))
    good.text(40, 40, "REAL CONTENT", (255, 255, 255), scale=6)
    blank_bytes = encode_png(Raster(400, 400, (10, 10, 10)))

    with pytest.raises(RenderGateFailed) as exc:
        assert_render_ok(good, blank_bytes, expected=(400, 400))
    assert "distinct colours" in str(exc.value)

    # And the decoder really does round-trip the pixels.
    assert decode_png(encode_png(good)).px == good.px


def test_corrupt_pixel_data_is_caught():
    from newsletter_autopilot.cover import decode_png

    data = bytearray(render_cover(ISSUE_DATE, "Test"))
    i = data.index(b"IDAT") + 4
    data[i:i + 8] = b"\x00" * 8                      # scribble on the stream
    with pytest.raises(RenderGateFailed):
        decode_png(bytes(data))
