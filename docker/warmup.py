"""Build-time warm-up: pay the engine's first-run costs once, in the image.

`invisible_playwright fetch` downloads the browser, but several first-launch
costs are not covered by it -- most importantly the GeoIP database the library
pulls to resolve the session locale from the egress IP. Left to runtime, the
first crawl after every `docker run` pays for them, which shows up as a slow
cold start before the worker can register with the gateway.

Launching once here populates those caches inside the image layer. Failure is
deliberately non-fatal: a build should not break because a network fetch was
unavailable, it should just produce a slower first start.
"""

import asyncio
import sys


async def warm() -> None:
    from invisible_playwright.async_api import InvisiblePlaywright

    async with InvisiblePlaywright(headless=True, humanize=False) as browser:
        page = await browser.new_page()
        await page.goto("about:blank")


try:
    asyncio.run(warm())
    print("warmup: engine caches populated")
except Exception as err:  # noqa: BLE001 - a build must not fail on this
    print(f"warmup: skipped ({type(err).__name__}: {err})", file=sys.stderr)
