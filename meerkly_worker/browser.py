"""The crawl engine: invisible_playwright (patched Firefox).

One page, one job at a time, behind a lock. The wait semantics come from
api-gateway/spec; see snippets.py.

Deliberate engine choices, do not "improve" without re-measuring:
  * No custom user agent, headers, viewport, or client-hint overrides. The
    library's engine-level patches are the whole fingerprint story and it
    actively fights UA overrides.
  * humanize=False — we never click, and the default adds up to 1.5s per
    pointer call.
  * headless comes from config. The image sets HEADLESS=true: invisible_playwright's
    own built-in Xvfb starts and manages the display when headless=True, so
    "headless" here really means headed-and-hidden, not a headless browser —
    we no longer run our own entrypoint-started Xvfb.
  * A stable seed from the machine id, so this worker presents one consistent
    fingerprint across restarts.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import time
from contextlib import AsyncExitStack
from pathlib import Path

from . import snippets
from .config import Config
from .fetch_spec import STABLE_QUIET_MS, effective_detect_probe, effective_settle_cap
from .identity import set_browser_version

NAVIGATION_TIMEOUT_MS = 30000
MAX_HTML_CHARS = 20_000_000
CRASH_RELAUNCH_DELAY_SEC = 1.0
# How often the CSP fallback re-serializes the DOM to look for changes.
STABLE_POLL_MS = 250

# Firefox's profile lock files (Chromium's are SingletonLock/Socket/Cookie).
PROFILE_LOCKS = ("lock", ".parentlock", "parent.lock")


def fingerprint_seed(machine_id: str) -> int:
    """A stable int31 fingerprint seed derived from the machine id."""
    return int.from_bytes(hashlib.sha256(machine_id.encode()).digest()[:4], "big") & 0x7FFFFFFF


def wait_branch(mode: str) -> str:
    """Which wait implementation a mode string selects."""
    if mode == "networkidle":
        return "networkidle"
    if mode == "domcontentloaded":
        return "none"
    if mode == "stable" or not mode:
        return "settle"
    return "selector"


def describe_error(err: BaseException) -> str:
    message = str(err)
    if re.search(r"Timeout .* exceeded", message, re.IGNORECASE):
        return f"Navigation timeout after {NAVIGATION_TIMEOUT_MS}ms"
    return f"Failed to load: {message.splitlines()[0]}"


def _is_destroyed_context(message: str | None) -> bool:
    """True when an evaluate failed because a navigation replaced the document.

    Firefox and Chromium word this differently, and the wording has changed
    across versions, so match on the shared idea rather than one exact string.
    """
    if not message:
        return False
    m = message.lower()
    return (
        "execution context" in m
        or "context was destroyed" in m
        or "browsingcontext is undefined" in m
    )


def _is_csp_blocked(message: str | None) -> bool:
    """True when an evaluate failed because the page's CSP forbids eval().

    Our snippets run in the page's main world, so a site sending
    `Content-Security-Policy: script-src` without 'unsafe-eval' blocks them.
    Playwright's isolated worlds would sidestep this; Firefox over Juggler has
    none.
    """
    if not message:
        return False
    m = message.lower()
    return "blocked by csp" in m or "unsafe-eval" in m or "content security policy" in m


def is_interrupted(err: BaseException) -> bool:
    """A goto aborted by a still-committing navigation from the previous job."""
    return re.search(r"interrupted by another", str(err), re.IGNORECASE) is not None


def failure_result(*, error: str, final_url: str | None, loaded_ms: int, status: int) -> dict:
    return {
        "success": False,
        "finalUrl": final_url,
        "title": None,
        "html": None,
        "error": error,
        "loadedMs": loaded_ms,
        "waitTimedOut": False,
        "matchedRule": -1,
        "httpStatus": status,
    }


def clear_profile_locks(profile_dir: Path, logger) -> None:
    """Remove lock files a previous run left behind.

    Unlink unconditionally: these are often DANGLING symlinks, so exists()
    reports False and a guard would skip them. A volume outliving its container
    otherwise makes Firefox believe another process owns the profile.
    """
    for name in PROFILE_LOCKS:
        try:
            (profile_dir / name).unlink()
        except FileNotFoundError:
            pass
        except OSError as err:
            logger.warn("Could not clear a stale profile lock", file=name, error=str(err))


class BrowserManager:
    def __init__(self, cfg: Config, machine_id: str, logger) -> None:
        self._cfg = cfg
        self._logger = logger
        self._profile_dir = cfg.home / "profile"
        self._seed = fingerprint_seed(machine_id)

        self._stack: AsyncExitStack | None = None
        self._context = None
        self._page = None
        self._closed = False
        self._launching: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        # Why the last exec_js fell back, so callers can report a cause.
        self._last_exec_error: str | None = None

    def is_ready(self) -> bool:
        return (
            not self._closed
            and self._context is not None
            and self._page is not None
            and not self._page.is_closed()
        )

    async def ensure_browser(self) -> None:
        if self._closed:
            raise RuntimeError("Browser manager is closed")
        if self.is_ready():
            return
        if self._launching is None or self._launching.done():
            self._launching = asyncio.create_task(self._launch())
        await self._launching

    async def _launch(self) -> None:
        from invisible_playwright.async_api import InvisiblePlaywright

        self._profile_dir.mkdir(parents=True, exist_ok=True)
        clear_profile_locks(self._profile_dir, self._logger)

        options: dict = {
            "seed": self._seed,
            "headless": self._cfg.headless,
            "humanize": False,
            "profile_dir": str(self._profile_dir),
        }
        if self._cfg.locale:
            options["locale"] = self._cfg.locale
        if self._cfg.timezone:
            options["timezone"] = self._cfg.timezone

        stack = AsyncExitStack()
        try:
            # profile_dir makes this yield a BrowserContext, not a Browser.
            context = await stack.enter_async_context(InvisiblePlaywright(**options))
        except BaseException:
            await stack.aclose()
            raise

        self._stack = stack
        self._context = context

        try:
            browser = context.browser
            if browser is not None:
                set_browser_version(browser.version)
        except Exception:
            pass  # not exposed on every persistent-context path

        context.on("close", self._on_context_closed)

        # ALWAYS open our own page; never adopt the one the persistent context
        # starts with. That initial page is about:home and its browsing context
        # is not usable -- goto on it fails with
        #   Protocol error (Page.navigate): can't access property "loadURI",
        #   browsingContext is undefined
        # The library's new_page() carries a settle for exactly this race.
        # Open first, then discard the stale page, so the context is never
        # momentarily empty (which would close it).
        stale = list(context.pages)
        self._page = await context.new_page()
        for page in stale:
            await self._close_quietly(page)

        self._page.on("crash", self._on_crash)
        self._page.set_default_timeout(NAVIGATION_TIMEOUT_MS)

        # Registered only AFTER the primary page exists. new_page() fires this
        # event, so subscribing earlier means the handler runs while _page is
        # still None and closes the page we just opened.
        context.on("page", self._on_extra_page)

        self._logger.info("Browser ready", headless=self._cfg.headless)

    def _on_extra_page(self, page) -> None:
        # Block window.open / target=_blank: one page, one job.
        # Never act while _page is None: during a relaunch or crash recovery
        # that would close the incoming page instead of adopting it.
        if self._page is not None and page is not self._page:
            asyncio.create_task(self._close_quietly(page))

    async def _close_quietly(self, page) -> None:
        try:
            await page.close()
        except Exception:
            pass

    def _on_context_closed(self, _context=None) -> None:
        self._context = None
        self._page = None
        if not self._closed:
            self._logger.warn("Browser context closed unexpectedly; relaunching on the next job")

    def _on_crash(self, _page=None) -> None:
        self._logger.error("Browser page crashed; recreating")
        self._page = None
        asyncio.create_task(self._recover())

    async def _recover(self) -> None:
        await self._teardown()
        # The delay keeps a crash loop from becoming a tight loop.
        await asyncio.sleep(CRASH_RELAUNCH_DELAY_SEC)
        if self._closed:
            return
        try:
            await self.ensure_browser()
        except Exception as err:
            self._logger.error("Relaunch after crash failed", error=str(err))

    async def _teardown(self) -> None:
        stack, self._stack = self._stack, None
        self._context = None
        self._page = None
        if stack is not None:
            try:
                await stack.aclose()
            except Exception:
                pass

    async def close(self) -> None:
        self._closed = True
        await self._teardown()

    # --- the crawl ---------------------------------------------------------

    async def navigate_and_extract(self, job: dict) -> dict:
        async with self._lock:
            return await self._fetch(job)

    async def _fetch(self, job: dict) -> dict:
        started = time.monotonic()
        deadline = started + NAVIGATION_TIMEOUT_MS / 1000
        elapsed_ms = lambda: int((time.monotonic() - started) * 1000)  # noqa: E731
        remaining_ms = lambda: max(0, int((deadline - time.monotonic()) * 1000))  # noqa: E731

        status = 0

        def fail(error: str, final_url: str | None) -> dict:
            # `status` is read live, so a failure after a committed navigation
            # still carries the real value.
            return failure_result(
                error=error, final_url=final_url, loaded_ms=elapsed_ms(), status=status
            )

        try:
            await self.ensure_browser()
        except Exception as err:
            return fail(f"Failed to launch browser: {err}", None)

        page = self._page
        if page is None:
            return fail("Failed to launch browser", None)

        def on_response(response) -> None:
            nonlocal status
            try:
                if response.request.is_navigation_request() and response.frame == page.main_frame:
                    status = response.status
            except Exception:
                pass  # detached-frame race

        page.on("response", on_response)
        try:
            nav_error = await self._goto(page, job["url"], remaining_ms)
            if nav_error is not None:
                await self._drain_error_page(page)
                return fail(nav_error, self._safe_url(page))

            matched_rule = -1
            rules = job["waitRules"]
            if rules:
                probe_ms = effective_detect_probe(job["detectMs"], remaining_ms())
                matched_rule = await self._probe_rules(
                    page, [rule["if"] for rule in rules], probe_ms
                )

            mode = rules[matched_rule]["then"] if matched_rule >= 0 else job["waitFor"]
            branch = wait_branch(mode)
            wait_timed_out = False

            if branch == "networkidle":
                wait_timed_out = await self._network_idle(page, remaining_ms())
            elif branch == "settle":
                await self._settle(page, job["settleMs"], remaining_ms())
            elif branch == "selector":
                wait_timed_out = await self._wait_selector_visible(page, mode, remaining_ms())
                if not wait_timed_out:
                    await self._settle(page, job["settleMs"], remaining_ms())
            # branch == "none": domcontentloaded, capture right away

            html = await self._extract_html(page, remaining_ms)
            if not isinstance(html, str):
                # Report why, not just that. A destroyed execution context and a
                # blown deadline need completely different responses, and a
                # single generic message made them indistinguishable in the
                # field.
                result = fail(
                    f"HTML extraction failed: {self._last_exec_error or 'unknown'}",
                    self._safe_url(page),
                )
                result["title"] = await self._safe_title(page)
                return result
            if len(html) >= MAX_HTML_CHARS:
                self._logger.warn("Extracted HTML truncated at cap", cap=MAX_HTML_CHARS)

            return {
                "success": True,
                "finalUrl": self._safe_url(page),
                "title": await self._safe_title(page),
                "html": html,
                "error": None,
                "loadedMs": elapsed_ms(),
                "waitTimedOut": wait_timed_out,
                "matchedRule": matched_rule,
                "httpStatus": status,
            }
        finally:
            try:
                page.remove_listener("response", on_response)
            except Exception:
                pass  # the page may be gone after a crash

    async def _extract_html(self, page, remaining_ms):
        """Read the page's HTML, retrying once if a navigation ate the context.

        Uses page.content() rather than an injected snippet. Our snippets run
        in the page's main world (Firefox over Juggler has no isolated world),
        so page.evaluate needs eval() in the page -- and any site sending
        `Content-Security-Policy: script-src` without 'unsafe-eval' blocks it
        outright with "call to eval() blocked by CSP". content() serializes the
        document over the protocol instead, so CSP does not apply.

        A client-side redirect or meta-refresh can still destroy the execution
        context between the wait finishing and this running, which fails
        instantly rather than timing out. The document that replaced it is
        usually the one worth capturing, so settle briefly and try again.
        """
        html = await self._page_content(page, remaining_ms())
        if isinstance(html, str):
            return html[:MAX_HTML_CHARS]

        if not _is_destroyed_context(self._last_exec_error) or remaining_ms() < 500:
            return None

        self._logger.debug(
            "Execution context was destroyed mid-extraction; retrying once",
            error=self._last_exec_error,
        )
        await self._settle(page, 0, min(remaining_ms(), 2000))
        html = await self._page_content(page, remaining_ms())
        return html[:MAX_HTML_CHARS] if isinstance(html, str) else None

    async def _wait_selector_visible(self, page, selector: str, budget_ms: int) -> bool:
        """Wait for `selector` to be visible. Returns True on timeout.

        Native Playwright rather than an injected snippet: our snippets run in
        the page's main world, so a site whose CSP forbids eval() blocks them.
        A selector that never matches -- including an invalid one -- times out,
        which is the spec's behaviour for a target selector.
        """
        try:
            await page.wait_for_selector(selector, state="visible", timeout=budget_ms)
            return False
        except Exception:
            return True

    async def _probe_rules(self, page, selectors: list[str], budget_ms: int) -> int:
        """Index of the first VISIBLE selector by list order, or -1 at budget.

        Native Playwright for the same CSP reason as _wait_selector_visible. A
        selector that throws is treated as not-found and probing continues --
        deliberately unlike the target-selector wait, because a bad guard
        should simply never match rather than abort the probe.
        """
        deadline = time.monotonic() + budget_ms / 1000
        while True:
            for index, selector in enumerate(selectors):
                try:
                    element = await page.query_selector(selector)
                    if element is not None and await element.is_visible():
                        return index
                except Exception:
                    continue
            if time.monotonic() >= deadline:
                return -1
            await asyncio.sleep(0.05)

    async def _page_content(self, page, budget_ms: int):
        """page.content() with the same never-raise contract as exec_js."""
        try:
            value = await asyncio.wait_for(page.content(), timeout=(budget_ms + 1000) / 1000)
        except TimeoutError:
            self._last_exec_error = f"timed out after {budget_ms + 1000}ms"
            return None
        except Exception as err:
            self._last_exec_error = str(err).splitlines()[0][:200]
            return None
        self._last_exec_error = None
        return value

    async def exec_js(self, page, expression: str, arg, budget_ms: int, fallback):
        """Evaluate in the page, resolving to `fallback` instead of raising.

        The failure reason is kept in _last_exec_error rather than discarded:
        callers need to distinguish a blown deadline from a destroyed context.

        The +1s grace lets the in-page cap timers win while the renderer is
        healthy. Load-bearing: the snippets run in the page's main world, so
        without this deadline a hostile page could stall a job indefinitely.
        """
        try:
            value = await asyncio.wait_for(
                page.evaluate(expression, arg), timeout=(budget_ms + 1000) / 1000
            )
        except TimeoutError:
            self._last_exec_error = f"timed out after {budget_ms + 1000}ms"
            return fallback
        except Exception as err:
            self._last_exec_error = str(err).splitlines()[0][:200]
            return fallback
        self._last_exec_error = None
        return value

    async def _settle(self, page, settle_ms: int, budget_ms: int) -> None:
        cap = effective_settle_cap(settle_ms, budget_ms)
        await self.exec_js(page, snippets.SETTLE_STABLE, cap, cap, None)

        if _is_csp_blocked(self._last_exec_error):
            self._logger.debug(
                "Page CSP blocks eval; settling by polling the DOM instead", capMs=cap
            )
            await self._settle_by_polling(page, cap)

    async def _settle_by_polling(self, page, cap_ms: int) -> None:
        """DOM-quiet detection without eval, for pages whose CSP forbids it.

        The MutationObserver snippet is the primary path and stays: it is
        cheap and precise. But when a page's CSP blocks eval it cannot run at
        all, and a fixed pause is far too weak -- content that arrives by XHR a
        few seconds after load (a search page's generated summary, say) gets
        captured before it lands, which reads as "the wait returned too early".

        So re-serialize the document over the protocol and treat it as settled
        once the content stops changing for the same STABLE_QUIET_MS window the
        snippet uses. Coarser and more expensive than observing mutations, but
        it actually waits for the page.
        """
        deadline = time.monotonic() + cap_ms / 1000
        previous = None
        last_change = time.monotonic()

        while time.monotonic() < deadline:
            html = await self._page_content(page, 5000)
            if not isinstance(html, str):
                return  # page went away; nothing to settle for

            digest = hashlib.sha256(html.encode("utf-8", "ignore")).digest()
            now = time.monotonic()
            if digest != previous:
                previous = digest
                last_change = now
            elif (now - last_change) * 1000 >= STABLE_QUIET_MS:
                return

            await asyncio.sleep(STABLE_POLL_MS / 1000)

    async def _network_idle(self, page, budget_ms: int) -> bool:
        try:
            await page.wait_for_load_state("networkidle", timeout=budget_ms)
            return False
        except Exception:
            return True

    async def _goto(self, page, url: str, remaining_ms) -> str | None:
        """None on success, otherwise a caller-facing error string."""
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=remaining_ms())
            return None
        except Exception as err:
            if not is_interrupted(err) or remaining_ms() < 1000:
                return describe_error(err)
            self._logger.debug("Navigation interrupted by a stale one; retrying once", url=url)

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=remaining_ms())
            return None
        except Exception as err:
            return describe_error(err)

    async def _drain_error_page(self, page) -> None:
        """Absorb the error-page commit that lands AFTER goto rejects.

        The browser commits its network-error page after the goto has already
        failed; left in flight, that commit interrupts the NEXT job's
        navigation. Task 8 verifies this empirically — one unreachable URL must
        not poison the following crawls.
        """
        try:
            await page.wait_for_url(re.compile(r"^about:neterror"), timeout=500)
        except Exception:
            pass  # nothing to drain

    def _safe_url(self, page) -> str | None:
        try:
            return page.url
        except Exception:
            return None

    async def _safe_title(self, page) -> str | None:
        try:
            return await page.title()
        except Exception:
            return None
