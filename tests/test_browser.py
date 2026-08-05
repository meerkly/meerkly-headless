import pytest
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeout

from meerkly_worker.browser import (
    MAX_HTML_CHARS,
    NAVIGATION_TIMEOUT_MS,
    clear_profile_locks,
    describe_error,
    failure_result,
    fingerprint_seed,
    is_interrupted,
    wait_branch,
)
from meerkly_worker.log import get_logger

MACHINE = "3f2b7c1e-0000-4000-8000-000000000001"


@pytest.fixture
def log():
    return get_logger("error")


def test_budget_constants():
    assert NAVIGATION_TIMEOUT_MS == 30000
    assert MAX_HTML_CHARS == 20_000_000


def test_seed_is_stable_deterministic_and_int31():
    seed = fingerprint_seed(MACHINE)
    assert seed == fingerprint_seed(MACHINE)
    assert 0 <= seed <= 0x7FFFFFFF
    assert seed != fingerprint_seed("3f2b7c1e-0000-4000-8000-000000000002")


@pytest.mark.parametrize(
    "mode,branch",
    [
        ("stable", "settle"),
        ("", "settle"),
        ("domcontentloaded", "none"),
        ("networkidle", "networkidle"),
        ("#result", "selector"),
        (".card > a[href]", "selector"),
    ],
)
def test_wait_branch_selection(mode, branch):
    assert wait_branch(mode) == branch


def test_timeout_error_message():
    err = PlaywrightTimeout("Timeout 30000ms exceeded.\nCall log:\n  - navigating")
    assert describe_error(err) == "Navigation timeout after 30000ms"


def test_other_errors_use_only_the_first_line():
    err = PlaywrightError("NS_ERROR_UNKNOWN_HOST at https://nope.invalid/\nCall log:\n  - x")
    assert describe_error(err) == "Failed to load: NS_ERROR_UNKNOWN_HOST at https://nope.invalid/"


def test_interrupted_navigation_detection():
    assert is_interrupted(PlaywrightError("Navigation interrupted by another one"))
    assert is_interrupted(PlaywrightError("navigation interrupted by another navigation"))
    assert not is_interrupted(PlaywrightError("NS_ERROR_CONNECTION_REFUSED"))
    assert not is_interrupted(PlaywrightTimeout("Timeout 30000ms exceeded."))


def test_failure_result_is_the_wire_shape():
    result = failure_result(error="Failed to load: boom", final_url=None, loaded_ms=12, status=0)
    assert set(result) == {
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
    assert result["success"] is False
    assert result["waitTimedOut"] is False
    assert result["matchedRule"] == -1
    assert result["title"] is None and result["html"] is None


def test_failure_result_keeps_a_live_status():
    """A failure after a committed navigation still reports the real status."""
    result = failure_result(
        error="HTML extraction failed", final_url="https://example.com/", loaded_ms=900, status=404
    )
    assert result["httpStatus"] == 404
    assert result["finalUrl"] == "https://example.com/"


def test_clear_profile_locks_removes_firefox_locks(tmp_path, log):
    profile = tmp_path / "profile"
    profile.mkdir()
    for name in ("lock", ".parentlock", "parent.lock"):
        (profile / name).write_text("")
    (profile / "prefs.js").write_text("keep")

    clear_profile_locks(profile, log)

    assert not (profile / "lock").exists()
    assert not (profile / ".parentlock").exists()
    assert (profile / "prefs.js").exists()


def test_clear_profile_locks_removes_dangling_symlinks(tmp_path, log):
    """Firefox's lock is often a symlink to a dead target, so exists() is False
    and an existence guard would skip it."""
    profile = tmp_path / "profile"
    profile.mkdir()
    link = profile / "lock"
    link.symlink_to(tmp_path / "gone")
    assert not link.exists() and link.is_symlink()

    clear_profile_locks(profile, log)
    assert not link.is_symlink()


def test_clear_profile_locks_tolerates_a_missing_dir(tmp_path, log):
    clear_profile_locks(tmp_path / "nope", log)  # must not raise
