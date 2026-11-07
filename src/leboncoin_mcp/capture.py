"""Read data the way a person's browser does: open the page, keep the response it loads."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from playwright.async_api import Page, Response
from playwright.async_api import TimeoutError as PlaywrightTimeout

from .config import Settings
from .errors import BlockedByDataDome, UnexpectedResponse
from .guards import check_page


async def capture_json(
    page: Page, settings: Settings, url: str, matches: Callable[[Response], bool], what: str
) -> dict[str, Any]:
    """Open `url` and return the JSON of the first response accepted by `matches`."""
    try:
        # Arm the wait *before* navigating, or a fast response would be missed.
        async with page.expect_response(matches) as captured:
            await page.goto(url, wait_until="domcontentloaded")
        response = await captured.value
    except PlaywrightTimeout:
        await check_page(page, settings.novnc_url)
        raise UnexpectedResponse(f"The page loaded without {what}.") from None

    await check_page(page, settings.novnc_url)
    if response.status == 403:
        raise BlockedByDataDome(what, settings.novnc_url)
    if response.status != 200:
        raise UnexpectedResponse(f"{what}: HTTP {response.status}")
    return await response.json()
