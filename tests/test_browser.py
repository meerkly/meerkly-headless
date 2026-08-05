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


# --- exec_js error reporting -----------------------------------------------


def test_destroyed_context_matcher():
    from meerkly_worker.browser import _is_destroyed_context

    assert _is_destroyed_context("Execution context was destroyed")
    assert _is_destroyed_context("execution context was destroyed, most likely by a navigation")
    assert _is_destroyed_context('can\'t access property "loadURI", browsingContext is undefined')
    assert not _is_destroyed_context("timed out after 5000ms")
    assert not _is_destroyed_context("NS_ERROR_UNKNOWN_HOST")
    assert not _is_destroyed_context(None)
    assert not _is_destroyed_context("")


class _EvalPage(_FakePage):
    """A page whose evaluate() and content() outcomes are scripted per call."""

    def __init__(self, outcomes=(), content_outcomes=()):
        super().__init__()
        self.outcomes = list(outcomes)
        self.content_outcomes = list(content_outcomes)
        self.calls = 0
        self.content_calls = 0

    async def evaluate(self, expression, arg=None):
        self.calls += 1
        outcome = self.outcomes.pop(0) if self.outcomes else None
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def content(self):
        self.content_calls += 1
        outcome = self.content_outcomes.pop(0) if self.content_outcomes else None
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


async def test_exec_js_records_why_it_fell_back(tmp_path, log):
    from meerkly_worker.browser import BrowserManager

    manager = BrowserManager(_config(tmp_path), MACHINE, log)
    page = _EvalPage([RuntimeError("Execution context was destroyed\nsecond line ignored")])

    result = await manager.exec_js(page, "() => 1", None, 100, "FALLBACK")

    assert result == "FALLBACK"
    assert manager._last_exec_error == "Execution context was destroyed"


async def test_exec_js_clears_the_error_on_success(tmp_path, log):
    from meerkly_worker.browser import BrowserManager

    manager = BrowserManager(_config(tmp_path), MACHINE, log)
    manager._last_exec_error = "stale"

    assert await manager.exec_js(_EvalPage(["ok"]), "() => 1", None, 100, None) == "ok"
    assert manager._last_exec_error is None


async def test_extraction_uses_content_not_eval_so_csp_cannot_block_it(tmp_path, log):
    """Regression: extraction via page.evaluate needs eval() in the page, and a
    site sending CSP without 'unsafe-eval' rejects it with
    'call to eval() blocked by CSP'. page.content() goes over the protocol."""
    from meerkly_worker.browser import BrowserManager

    manager = BrowserManager(_config(tmp_path), MACHINE, log)
    page = _EvalPage(
        outcomes=[RuntimeError("call to eval() blocked by CSP")],
        content_outcomes=["<html>captured anyway</html>"],
    )

    assert await manager._extract_html(page, lambda: 10_000) == "<html>captured anyway</html>"
    assert page.content_calls == 1
    assert page.calls == 0, "extraction must not evaluate at all"


async def test_extraction_retries_once_when_a_navigation_ate_the_context(tmp_path, log):
    """A client-side redirect destroys the context between the wait and the
    extract. The replacement document is the one worth capturing."""
    from meerkly_worker.browser import BrowserManager

    manager = BrowserManager(_config(tmp_path), MACHINE, log)
    page = _EvalPage(
        outcomes=[None],  # the settle between attempts
        content_outcomes=[
            RuntimeError("Execution context was destroyed"),
            "<html>after redirect</html>",
        ],
    )

    html = await manager._extract_html(page, lambda: 10_000)

    assert html == "<html>after redirect</html>"
    assert page.content_calls == 2


async def test_extraction_does_not_retry_other_failures(tmp_path, log):
    """A timeout means the page is wedged; retrying just burns the budget."""
    from meerkly_worker.browser import BrowserManager

    manager = BrowserManager(_config(tmp_path), MACHINE, log)
    page = _EvalPage(content_outcomes=[RuntimeError("NS_ERROR_FAILURE")])

    assert await manager._extract_html(page, lambda: 10_000) is None
    assert page.content_calls == 1


async def test_extraction_does_not_retry_without_budget(tmp_path, log):
    from meerkly_worker.browser import BrowserManager

    manager = BrowserManager(_config(tmp_path), MACHINE, log)
    page = _EvalPage(content_outcomes=[RuntimeError("Execution context was destroyed")])

    assert await manager._extract_html(page, lambda: 100) is None
    assert page.content_calls == 1, "must not retry with no budget left"


async def test_extraction_caps_oversized_html(tmp_path, log):
    from meerkly_worker.browser import MAX_HTML_CHARS, BrowserManager

    manager = BrowserManager(_config(tmp_path), MACHINE, log)
    page = _EvalPage(content_outcomes=["x" * (MAX_HTML_CHARS + 5000)])

    assert len(await manager._extract_html(page, lambda: 10_000)) == MAX_HTML_CHARS


def test_csp_blocked_matcher():
    from meerkly_worker.browser import _is_csp_blocked

    assert _is_csp_blocked("Page.evaluate: call to eval() blocked by CSP")
    assert _is_csp_blocked("EvalError: refused to evaluate, unsafe-eval missing")
    assert _is_csp_blocked("violates Content Security Policy directive")
    assert not _is_csp_blocked("Execution context was destroyed")
    assert not _is_csp_blocked("timed out after 5000ms")
    assert not _is_csp_blocked(None)


async def test_settle_polls_the_dom_when_csp_blocks_eval(tmp_path, log):
    """Under a CSP that forbids eval the MutationObserver settle cannot run, so
    we poll the serialized DOM instead. It must keep waiting while content is
    still arriving -- a fixed pause captured XHR content too early."""
    from meerkly_worker.browser import BrowserManager

    manager = BrowserManager(_config(tmp_path), MACHINE, log)
    # Content keeps changing for three polls, then goes quiet.
    page = _EvalPage(
        outcomes=[RuntimeError("call to eval() blocked by CSP")],
        content_outcomes=["<a>", "<ab>", "<abc>"] + ["<abc>"] * 20,
    )

    await manager._settle(page, 0, 10_000)

    assert page.content_calls >= 5, "should have polled until the DOM stopped changing"
    # Stops once quiet, rather than burning the whole cap.
    assert page.content_calls < 20, "should stop polling once settled"


async def test_settle_polling_respects_the_cap(tmp_path, log):
    """A page that never goes quiet must still return at the cap."""
    import itertools
    import time as _time

    from meerkly_worker.browser import BrowserManager

    manager = BrowserManager(_config(tmp_path), MACHINE, log)
    counter = itertools.count()
    page = _EvalPage(outcomes=[RuntimeError("call to eval() blocked by CSP")])
    page.content = lambda: _forever_changing(counter)  # noqa: E731

    started = _time.monotonic()
    await manager._settle(page, 0, 1200)
    elapsed_ms = (_time.monotonic() - started) * 1000

    assert 1100 <= elapsed_ms < 3000, f"should stop at the cap, took {elapsed_ms:.0f}ms"


async def _forever_changing(counter):
    return f"<html>{next(counter)}</html>"


async def test_settle_does_not_pause_when_eval_works(tmp_path, log):
    import time as _time

    from meerkly_worker.browser import BrowserManager

    manager = BrowserManager(_config(tmp_path), MACHINE, log)
    page = _EvalPage(outcomes=[None])

    started = _time.monotonic()
    await manager._settle(page, 0, 10_000)
    assert (_time.monotonic() - started) * 1000 < 300


# --- JSON passthrough -------------------------------------------------------


def test_json_content_type_matcher():
    from meerkly_worker.browser import _is_json_content_type

    for value in [
        "application/json",
        "application/json; charset=utf-8",
        "APPLICATION/JSON",
        "text/json",
        "application/problem+json",
        "application/ld+json; charset=utf-8",
    ]:
        assert _is_json_content_type(value), value
    for value in ["text/html", "text/html; charset=utf-8", "application/xml", "", "text/plain"]:
        assert not _is_json_content_type(value), value


class _FakeResponse:
    def __init__(self, headers, body=None, raises=False):
        self.headers = headers
        self._body = body
        self._raises = raises
        self.text_calls = 0

    async def text(self):
        self.text_calls += 1
        if self._raises:
            raise RuntimeError("body already discarded")
        return self._body


async def test_json_response_returns_the_raw_body(tmp_path, log):
    """A browser renders JSON in a viewer; the caller asked for the data."""
    from meerkly_worker.browser import BrowserManager

    manager = BrowserManager(_config(tmp_path), MACHINE, log)
    response = _FakeResponse({"content-type": "application/json"}, body='{"ok":true}')

    assert await manager._raw_body_if_json(None, response) == '{"ok":true}'


async def test_html_response_is_left_to_the_renderer(tmp_path, log):
    from meerkly_worker.browser import BrowserManager

    manager = BrowserManager(_config(tmp_path), MACHINE, log)
    response = _FakeResponse({"content-type": "text/html; charset=utf-8"}, body="<html>")

    assert await manager._raw_body_if_json(None, response) is None
    assert response.text_calls == 0, "must not read the body for HTML"


async def test_missing_response_is_handled(tmp_path, log):
    from meerkly_worker.browser import BrowserManager

    manager = BrowserManager(_config(tmp_path), MACHINE, log)
    assert await manager._raw_body_if_json(None, None) is None
    assert await manager._raw_body_if_json(None, _FakeResponse({})) is None


async def test_unavailable_json_body_falls_back(tmp_path, log):
    """Better a rendered document than a failed crawl."""
    from meerkly_worker.browser import BrowserManager

    manager = BrowserManager(_config(tmp_path), MACHINE, log)
    response = _FakeResponse({"content-type": "application/json"}, raises=True)
    page = _EvalPage()  # no #json / <pre> to recover from either

    async def no_element(selector):
        return None

    page.query_selector = no_element
    assert await manager._raw_body_if_json(page, response) is None


async def test_oversized_json_is_capped(tmp_path, log):
    from meerkly_worker.browser import MAX_HTML_CHARS, BrowserManager

    manager = BrowserManager(_config(tmp_path), MACHINE, log)
    response = _FakeResponse(
        {"content-type": "application/json"}, body="x" * (MAX_HTML_CHARS + 1000)
    )

    assert len(await manager._raw_body_if_json(None, response)) == MAX_HTML_CHARS


async def test_json_recovered_from_the_firefox_viewer_dom(tmp_path, log):
    """The real failure mode: the viewer consumes the stream, so
    response.text() raises NS_ERROR_FAILURE and the payload only survives in
    the viewer's own DOM node."""
    from meerkly_worker.browser import BrowserManager

    manager = BrowserManager(_config(tmp_path), MACHINE, log)
    response = _FakeResponse({"content-type": "application/json"}, raises=True)

    class _El:
        def __init__(self, text):
            self._text = text

        async def inner_text(self):
            return self._text

    page = _EvalPage()

    async def query_selector(selector):
        return _El('{"ip":"1.2.3.4"}') if selector == "#json" else None

    page.query_selector = query_selector

    assert await manager._raw_body_if_json(page, response) == '{"ip":"1.2.3.4"}'


async def test_json_recovered_from_a_pre_element(tmp_path, log):
    """Chromium renders JSON into a bare <pre> rather than a viewer."""
    from meerkly_worker.browser import BrowserManager

    manager = BrowserManager(_config(tmp_path), MACHINE, log)
    response = _FakeResponse({"content-type": "application/json"}, raises=True)

    class _El:
        async def inner_text(self):
            return '{"from":"pre"}'

    page = _EvalPage()

    async def query_selector(selector):
        return _El() if selector == "pre" else None

    page.query_selector = query_selector

    assert await manager._raw_body_if_json(page, response) == '{"from":"pre"}'
