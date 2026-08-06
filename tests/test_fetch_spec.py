import json
import math

import pytest

from meerkly_headless import fetch_spec


def test_constants_match_the_schema(spec_dir):
    constants = json.loads((spec_dir / "fetch-job.schema.json").read_text())["$defs"]["constants"]
    assert fetch_spec.WAIT_FOR_DEFAULT == constants["waitForDefault"]
    assert fetch_spec.SETTLE_DEFAULT_MS == constants["settleDefaultMs"]
    assert fetch_spec.SETTLE_MAX_MS == constants["settleMaxMs"]
    assert fetch_spec.DETECT_DEFAULT_MS == constants["detectDefaultMs"]
    assert fetch_spec.DETECT_MAX_MS == constants["detectMaxMs"]
    assert fetch_spec.STABLE_QUIET_MS == constants["stableQuietMs"]


def test_every_vector_case(load_vector):
    cases = load_vector("fetch-frames.json")["cases"]
    assert cases, "vector file is empty"

    for case in cases:
        parsed = fetch_spec.parse_fetch_frame(case["frame"])
        assert parsed == case["parsed"], case["name"]

        effective = case["effective"]
        assert fetch_spec.effective_settle_cap(parsed["settleMs"]) == effective["settleCapMs"]
        assert fetch_spec.effective_detect_probe(parsed["detectMs"]) == effective["detectProbeMs"]


@pytest.mark.parametrize(
    "frame",
    [
        None,
        "fetch",
        42,
        [],
        {"type": "fetch", "url": "https://example.com"},
        {"type": "fetch", "jobId": "j1"},
        {"type": "fetch", "jobId": "", "url": "https://example.com"},
        {"type": "fetch", "jobId": "j1", "url": ""},
        {"type": "fetch", "jobId": 1, "url": "https://example.com"},
        {"type": "fetch", "jobId": "j1", "url": None},
    ],
)
def test_malformed_frames_parse_to_none(frame):
    assert fetch_spec.parse_fetch_frame(frame) is None


def test_values_are_not_clamped_at_parse_time():
    parsed = fetch_spec.parse_fetch_frame(
        {"jobId": "j", "url": "u", "settleMs": 99999, "detectMs": 99999}
    )
    assert parsed["settleMs"] == 99999
    assert parsed["detectMs"] == 99999
    assert fetch_spec.effective_settle_cap(99999) == fetch_spec.SETTLE_MAX_MS
    assert fetch_spec.effective_detect_probe(99999) == fetch_spec.DETECT_MAX_MS


def test_negative_and_float_values():
    parsed = fetch_spec.parse_fetch_frame(
        {"jobId": "j", "url": "u", "settleMs": -5, "detectMs": 1200.9}
    )
    assert parsed["settleMs"] == 0
    assert parsed["detectMs"] == 1200


def test_booleans_are_not_numbers():
    """bool subclasses int in Python — settleMs: true must not become 1."""
    parsed = fetch_spec.parse_fetch_frame({"jobId": "j", "url": "u", "settleMs": True})
    assert parsed["settleMs"] == 0


def test_rules_without_a_guard_are_dropped_and_order_survives():
    parsed = fetch_spec.parse_fetch_frame(
        {
            "jobId": "j",
            "url": "u",
            "waitRules": [
                {"if": "#first", "then": "#a"},
                {"then": "#orphan"},
                {"if": "", "then": "#empty"},
                {"if": "#second"},
                {"if": "#third", "then": 42},
                "not-an-object",
            ],
        }
    )
    assert parsed["waitRules"] == [
        {"if": "#first", "then": "#a"},
        {"if": "#second", "then": ""},
        {"if": "#third", "then": ""},
    ]


def test_wait_rules_that_are_not_a_list_are_ignored():
    assert (
        fetch_spec.parse_fetch_frame({"jobId": "j", "url": "u", "waitRules": "no"})["waitRules"]
        == []
    )


@pytest.mark.parametrize("value", ["", None, 42])
def test_blank_wait_for_becomes_stable(value):
    parsed = fetch_spec.parse_fetch_frame({"jobId": "j", "url": "u", "waitFor": value})
    assert parsed["waitFor"] == fetch_spec.WAIT_FOR_DEFAULT


def test_budget_is_a_third_clamp():
    assert fetch_spec.effective_settle_cap(0, 1200) == 1200
    assert fetch_spec.effective_settle_cap(30000, 1200) == 1200
    assert fetch_spec.effective_detect_probe(0, 50) == 50
    assert fetch_spec.effective_settle_cap(0, math.inf) == fetch_spec.SETTLE_DEFAULT_MS
    assert isinstance(fetch_spec.effective_settle_cap(0, 1200.7), int)
