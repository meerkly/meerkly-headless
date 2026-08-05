import asyncio

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


# --- primary-page survival -------------------------------------------------


def _config(tmp_path):
    from meerkly_worker.config import Config

    return Config(
        gateway_url="wss://g/v1/connect",
        account_base_url="https://a",
        api_key=None,
        worker_id="w",
        worker_name="w",
        machine_id_override=None,
        home=tmp_path,
        headless=True,
        health_port=0,
        log_level="error",
        locale=None,
        timezone=None,
        allow_insecure=False,
    )


class _FakePage:
    def __init__(self, url="about:blank"):
        self.closed = False
        self.handlers = {}
        self.url = url

    def on(self, event, handler):
        self.handlers[event] = handler

    def set_default_timeout(self, ms):
        self.timeout = ms

    def is_closed(self):
        return self.closed

    async def close(self):
        self.closed = True


class _FakeContext:
    """Fires the 'page' event from new_page(), the way Playwright really does.

    `initial` models the persistent context's about:home page, which the real
    engine hands back already broken.
    """

    def __init__(self, initial=()):
        self.pages = [_FakePage(url) for url in initial]
        self._handlers = {}
        self.browser = None

    def on(self, event, handler):
        self._handlers[event] = handler

    async def new_page(self):
        page = _FakePage()
        self.pages.append(page)
        handler = self._handlers.get("page")
        if handler is not None:
            handler(page)
        return page


class _FakeInvisiblePlaywright:
    """Stands in for the engine so _launch itself can be exercised.

    Like the real persistent context, it comes up with an about:home page.
    """

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.context = _FakeContext(initial=["about:home"])

    async def __aenter__(self):
        return self.context

    async def __aexit__(self, *exc):
        return False


async def test_launch_does_not_close_the_page_it_just_created(tmp_path, log, monkeypatch):
    """Regression: _launch subscribed to 'page' before the primary page
    existed, so new_page() fired the handler while _page was still None, it
    judged that page a stray popup and closed it, and goto then failed with
    'browsingContext is undefined'.

    This drives the real _launch rather than replaying its steps, so
    re-ordering those lines fails the test.
    """
    import invisible_playwright.async_api as engine

    from meerkly_worker.browser import BrowserManager

    monkeypatch.setattr(engine, "InvisiblePlaywright", _FakeInvisiblePlaywright)

    manager = BrowserManager(_config(tmp_path), MACHINE, log)
    await manager._launch()
    await asyncio.sleep(0.01)

    assert manager._page is not None, "no page was adopted"
    assert not manager._page.is_closed(), "the primary page was closed at launch"
    assert manager.is_ready()


async def test_launch_never_adopts_the_contexts_initial_page(tmp_path, log, monkeypatch):
    """Regression: the persistent context starts on about:home, whose browsing
    context is unusable -- goto against it fails with 'browsingContext is
    undefined'. We must open our own page and discard that one."""
    import invisible_playwright.async_api as engine

    from meerkly_worker.browser import BrowserManager

    monkeypatch.setattr(engine, "InvisiblePlaywright", _FakeInvisiblePlaywright)

    manager = BrowserManager(_config(tmp_path), MACHINE, log)
    await manager._launch()
    await asyncio.sleep(0.01)

    assert manager._page.url != "about:home", "adopted the context's broken initial page"
    assert manager._page.url == "about:blank", "should be a page we opened ourselves"

    initial = [p for p in manager._context.pages if p.url == "about:home"]
    assert initial and initial[0].is_closed(), "the stale about:home page was left open"
    assert not manager._page.is_closed()
    assert manager.is_ready()


async def test_launch_passes_the_stealth_options(tmp_path, log, monkeypatch):
    """humanize must stay off and the fingerprint seed must be the stable one."""
    import invisible_playwright.async_api as engine

    from meerkly_worker.browser import BrowserManager, fingerprint_seed

    created = {}

    class _Recording(_FakeInvisiblePlaywright):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            created.update(kwargs)

    monkeypatch.setattr(engine, "InvisiblePlaywright", _Recording)

    await BrowserManager(_config(tmp_path), MACHINE, log)._launch()

    assert created["humanize"] is False
    assert created["seed"] == fingerprint_seed(MACHINE)
    assert created["headless"] is True
    assert created["profile_dir"].endswith("profile")
    # No user agent, viewport, or header overrides -- the engine's own patches
    # are the whole fingerprint story.
    assert not {"user_agent", "viewport", "extra_http_headers"} & set(created)


async def test_extra_pages_are_still_closed(tmp_path, log):
    """The popup killer must keep working once the primary page exists."""
    from meerkly_worker.browser import BrowserManager

    cfg = _config(tmp_path)
    manager = BrowserManager(cfg, MACHINE, log)
    context = _FakeContext()
    manager._page = await context.new_page()
    context.on("page", manager._on_extra_page)

    popup = await context.new_page()
    await asyncio.sleep(0.01)
    assert popup.is_closed(), "a window.open popup should be closed"
    assert not manager._page.is_closed(), "the primary page must survive"


def test_extra_page_handler_is_inert_while_page_is_none(tmp_path, log):
    """During crash recovery _page is transiently None; closing then would
    destroy the replacement page instead of adopting it."""
    from meerkly_worker.browser import BrowserManager

    cfg = _config(tmp_path)
    manager = BrowserManager(cfg, MACHINE, log)
    manager._page = None
    incoming = _FakePage()
    manager._on_extra_page(incoming)
    assert not incoming.is_closed()
