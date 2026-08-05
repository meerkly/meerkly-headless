import json

import pytest
from jsonschema import Draft202012Validator

from meerkly_worker import gateway as gw

MACHINE = "3f2b7c1e-0000-4000-8000-000000000001"


@pytest.fixture(scope="session")
def ws_schema(spec_dir):
    return json.loads((spec_dir / "ws-frames.schema.json").read_text())


def validator_for(schema, name):
    return Draft202012Validator({**schema["$defs"][name], "$defs": schema["$defs"]})


# --- conformance against the canonical vectors ------------------------------


def test_all_ws_frame_vectors(load_vector, ws_schema):
    cases = load_vector("ws-frames.json")["cases"]
    assert cases, "vector file is empty"
    for case in cases:
        actual = validator_for(ws_schema, case["def"]).is_valid(case["frame"])
        assert actual == case["valid"], case["name"]


def test_our_register_frames_validate(ws_schema):
    validator = validator_for(ws_schema, "register")
    validator.validate(gw.build_register(MACHINE, "dt_example"))
    validator.validate(gw.build_register(MACHINE, None))


def test_our_success_result_validates(ws_schema):
    frame = gw.build_result(
        "j1",
        {
            "success": True,
            "finalUrl": "https://example.com/",
            "title": "Example",
            "html": "<html></html>",
            "error": None,
            "loadedMs": 812,
            "waitTimedOut": False,
            "matchedRule": 0,
            "httpStatus": 200,
        },
    )
    validator_for(ws_schema, "result").validate(frame)


def test_our_failure_result_validates(ws_schema):
    """Nulls on the failure path were a real source of schema drift."""
    frame = gw.build_result(
        "j2",
        {
            "success": False,
            "finalUrl": None,
            "title": None,
            "html": None,
            "error": "Navigation timeout after 30000ms",
            "loadedMs": None,
            "waitTimedOut": False,
            "matchedRule": -1,
            "httpStatus": 0,
        },
    )
    validator_for(ws_schema, "result").validate(frame)


# --- frame construction -----------------------------------------------------


def test_register_frame_fields():
    frame = gw.build_register(MACHINE, "dt_x")
    assert frame["type"] == "register"
    assert frame["machineId"] == MACHINE
    assert frame["platform"] == "server"
    assert frame["capabilities"] == ["fetch"]
    # The gateway derives region from the observed IP and ignores this.
    assert frame["region"] == ""
    assert frame["deviceToken"] == "dt_x"


def test_device_token_key_is_omitted_when_absent():
    assert "deviceToken" not in gw.build_register(MACHINE, None)


def test_result_frame_always_carries_every_field():
    frame = gw.build_result("j1", {"success": True})
    assert set(frame) == {
        "type",
        "jobId",
        "success",
        "finalUrl",
        "title",
        "html",
        "error",
        "loadedMs",
        "waitTimedOut",
        "matchedRule",
        "httpStatus",
    }
    assert frame["finalUrl"] is None
    assert frame["waitTimedOut"] is False
    assert frame["matchedRule"] == -1
    assert frame["httpStatus"] == 0


# --- backoff ----------------------------------------------------------------


def test_backoff_doubles_to_a_ceiling():
    assert gw.next_backoff_ms(gw.INITIAL_BACKOFF_MS) == 2000
    assert gw.next_backoff_ms(16000) == 30000
    assert gw.next_backoff_ms(30000) == 30000


def test_backoff_constants():
    assert gw.INITIAL_BACKOFF_MS == 1000
    assert gw.MAX_BACKOFF_MS == 30000
    assert gw.BACKOFF_RESET_AFTER_MS == 10000
    assert gw.MAX_PENDING_JOBS == 3
