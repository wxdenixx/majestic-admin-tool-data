"""
Playwright-based bypass for the forum.majestic-rp.ru R3ACTLB anti-DDoS
challenge. Usage:

    from solver.cookie_solver import fetch_html

    html = await fetch_html("https://forum.majestic-rp.ru/threads/...")
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Optional

from playwright.async_api import async_playwright, Browser, BrowserContext

log = logging.getLogger(__name__)


@dataclass
class FetchResult:
    url: str
    html: str
    status: int


class ForumFetcher:
    """
    Opens a single Chromium context, lets the JS-challenge auto-execute,
    and then reuses the cookie jar for subsequent threads.
    """

    def __init__(
        self,
        user_agent: str,
        timeout_seconds: float = 60.0,
        wait_selector: str = "article.message .bbWrapper",
    ):
        self._user_agent = user_agent
        self._timeout = timeout_seconds * 1000  # Playwright uses ms
        self._wait_selector = wait_selector
        self._playwright = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None

    async def __aenter__(self) -> "ForumFetcher":
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        self._context = await self._browser.new_context(
            user_agent=self._user_agent,
            viewport={"width": 1366, "height": 900},
            locale="ru-RU",
        )
        return self

    async def __aexit__(self, exc_type, exc_value, tb):
        if self._context:
            await self._context.close()
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()

    async def fetch(self, url: str) -> FetchResult:
        assert self._context is not None
        page = await self._context.new_page()
        try:
            log.info("fetching %s", url)
            response = await page.goto(url, timeout=self._timeout, wait_until="domcontentloaded")
            status = response.status if response else 0

            # wait up to timeout for the challenge to clear
            try:
                await page.wait_for_selector(self._wait_selector, timeout=self._timeout)
            except Exception:
                # maybe the challenge bounces us back — give it one retry
                log.warning("selector %s missing, waiting 5s and retrying", self._wait_selector)
                await asyncio.sleep(5)
                await page.reload(timeout=self._timeout, wait_until="domcontentloaded")
                await page.wait_for_selector(self._wait_selector, timeout=self._timeout)

            html = await page.content()
            return FetchResult(url=url, html=html, status=status)
        finally:
            await page.close()


async def fetch_all(
    urls: list[str],
    user_agent: str,
    timeout_seconds: float = 60.0,
    wait_selector: str = "article.message .bbWrapper",
    delay_between_seconds: float = 2.0,
) -> list[FetchResult]:
    """
    Convenience wrapper: fetches every URL sequentially inside ONE Chromium
    context, so the cookie is reused. Between requests we sleep briefly to
    avoid looking like a burst-scraper.
    """
    results: list[FetchResult] = []
    async with ForumFetcher(user_agent, timeout_seconds, wait_selector) as fetcher:
        for url in urls:
            results.append(await fetcher.fetch(url))
            await asyncio.sleep(delay_between_seconds)
    return results
