"""Structural page checks. Never match on page text: real listing titles contain « robot »."""

from __future__ import annotations

from urllib.parse import urlsplit

from playwright.async_api import Page

from .errors import CaptchaRequired, NotAuthenticated

CAPTCHA_SELECTOR = (
    'iframe[src*="captcha-delivery.com"],iframe[src*="geo.captcha"],'
    '#ddChallengeContainer,[class*="ddCaptcha"]'
)
LOGIN_SELECTOR = "input[name=email],input[name=password]"


def is_login_url(url: str) -> bool:
    parts = urlsplit(url)
    return parts.hostname == "auth.leboncoin.fr" or parts.path.startswith(("/login", "/se-connecter"))


async def check_page(page: Page, novnc_url: str) -> None:
    """Raise if the page shows a challenge or a login form."""
    if await page.locator(CAPTCHA_SELECTOR).count():
        raise CaptchaRequired(novnc_url)
    if is_login_url(page.url) or await page.locator(LOGIN_SELECTOR).count():
        raise NotAuthenticated(novnc_url)
