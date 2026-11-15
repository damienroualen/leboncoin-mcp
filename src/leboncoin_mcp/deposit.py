"""Create an ad through the real deposit form, like a person.

The form is built from Leboncoin's own config (see adform.py). We type the title, pick the
category, choose the address among Leboncoin's suggestions, then fill each step field by field,
upload the photos and continue. Two network guards protect the end: the create request must carry
exactly the approved ad, and the final submit must carry no paid option.
"""

from __future__ import annotations

import asyncio
import json
import mimetypes
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page, Request, Response

from . import adform
from .adform import Field
from .config import Settings
from .errors import ActionRefused, ActionUnconfirmed, BlockedByDataDome, UnexpectedResponse
from .guards import check_page
from .netguard import RequestGuard

DEPOSIT_URL = "https://www.leboncoin.fr/deposer-une-annonce"
CONFIG_PATH = "/api/adsubmit/dynamic-deposit/config"
CREATE_PATH = "/api/adsubmit/v2/classifieds"
SUBMIT_PATH = "/api/services/v4/submit"
UPLOAD_PATH = "/api/pintad/v1/public/upload/image"
CATEGORIES = {"10": "Locations", "11": "Colocations"}
PHOTO_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}
MAX_PHOTOS = 30
MAX_PHOTO_BYTES = 20 * 1024 * 1024
UI_TIMEOUT_MS = 10_000
TYPING_DELAY_MS = 40  # a person types; a script pasting everything at once stands out
STEP_PAUSE_MS = 800


# --- inputs, checked before touching the browser ------------------------------------------


def check_category(category: str) -> str:
    if category not in CATEGORIES:
        raise ActionRefused(f"category must be one of {CATEGORIES} (rentals only, for now).")
    return category


def photo_files(photos_dir: str, names: list[str]) -> list[Path]:
    """Resolve photo names inside the photos folder, refusing anything outside it."""
    if not names:
        raise ActionRefused("Add at least one photo (file names in the photos folder).")
    if len(names) > MAX_PHOTOS:
        raise ActionRefused(f"At most {MAX_PHOTOS} photos.")
    root = Path(photos_dir).resolve()
    files, problems = [], []
    for name in names:
        path = (root / name).resolve()
        if not path.is_relative_to(root) or path == root:
            problems.append(f"{name}: outside the photos folder")
        elif path.suffix.lower() not in PHOTO_TYPES:
            problems.append(f"{name}: not a JPEG, PNG or WebP photo")
        elif not path.is_file():
            problems.append(f"{name}: no such file in the photos folder")
        elif path.stat().st_size > MAX_PHOTO_BYTES:
            problems.append(f"{name}: larger than {MAX_PHOTO_BYTES // 2**20} MB")
        else:
            files.append(path)
    if len(set(files)) != len(files):
        problems.append("the same photo is listed twice")
    if problems:
        raise ActionRefused("Photos refused, nothing was done:\n- " + "\n- ".join(problems))
    return files


def ad_values(title: str, description: str, price: int, attributes: dict[str, Any], hide_phone: bool):
    """All the form values of an ad, by field key, as the client gave them."""
    return {**attributes, "subject": title, "body": description, "price": price, "phone_hidden": hide_phone}


# --- the guards ----------------------------------------------------------------------------


def create_refusal(body: bytes | None, approved: dict[str, Any]) -> str | None:
    """Why the page's create request must be blocked, or None if it is exactly the approved ad."""
    try:
        payload = json.loads(body or b"")
    except ValueError:
        return "the create request was not the expected JSON"
    if not isinstance(payload, dict):
        return "the create request was not the expected JSON"
    for key in ("category_id", "subject", "body"):
        if str(payload.get(key)) != str(approved[key]):
            return f"its {key} differs from the approved ad"
    if str(payload.get("price")) != str(approved["price"]):
        return "its price differs from the approved ad"
    if len(payload.get("images") or []) != approved["photo_count"]:
        return "its number of photos differs from the approved ad"
    if (payload.get("location") or {}).get("label") != approved["address"]:
        return "its address differs from the approved one"
    if bool(payload.get("phone_hidden")) != approved["hide_phone"]:
        return "its phone visibility differs from the approved one"
    attributes = payload.get("attributes") or {}
    extended = payload.get("extended_attributes") or {}
    for key, values in approved["attributes"].items():
        sent = extended.get(key) if key in extended else attributes.get(key)
        sent_values = sorted(str(v) for v in sent) if isinstance(sent, list) else [str(sent)]
        if sent_values != sorted(values):
            return f"its {key} differs from the approved value"
    return None


def submit_refusal(body: bytes | None, ad_id: int | None) -> str | None:
    """The final submit may only publish the new ad, with no paid option."""
    try:
        payload = json.loads(body or b"")
    except ValueError:
        return "the submit request was not the expected JSON"
    ads = payload.get("ads") if isinstance(payload, dict) else None
    if not isinstance(ads, list) or len(ads) != 1:
        return "the submit request does not name exactly one ad"
    if ad_id is None or ads[0].get("ad_id") != ad_id:
        return "the submit request names another ad"
    if ads[0].get("options"):
        return "the submit request includes paid options"
    return None


class CreateGuard(RequestGuard):
    PATTERN = "**/api/adsubmit/v2/classifieds*"

    def __init__(self, approved: dict[str, Any]) -> None:
        def refusal(request: Request) -> str | None:
            if urlsplit(request.url).path != CREATE_PATH:
                return "the page sent an unexpected create request"
            return create_refusal(request.post_data_buffer, approved)

        super().__init__(("POST", "PUT", "PATCH"), refusal)


class SubmitGuard(RequestGuard):
    PATTERN = "**/api/services/**"

    def __init__(self) -> None:
        self.ad_id: int | None = None  # set once Leboncoin answers the create request

        def refusal(request: Request) -> str | None:
            if urlsplit(request.url).path != SUBMIT_PATH:
                return "the page tried to buy a service"
            return submit_refusal(request.post_data_buffer, self.ad_id)

        super().__init__(("POST", "PUT", "PATCH"), refusal)


# --- driving the form ----------------------------------------------------------------------


def _first_line(exc: Exception) -> str:
    return str(exc).splitlines()[0]


async def open_form(page: Page, settings: Settings, title: str, category: str) -> list[Field]:
    """Type the title, pick the category and return the form Leboncoin loads for it."""
    response = await page.goto(DEPOSIT_URL, wait_until="domcontentloaded")
    if response is not None and response.status == 403:
        raise BlockedByDataDome("the ad form", settings.novnc_url)
    await check_page(page, settings.novnc_url)
    subject = page.locator("input[name=subject]")
    await subject.wait_for(state="visible", timeout=UI_TIMEOUT_MS * 2)
    await subject.press_sequentially(title, delay=TYPING_DELAY_MS)
    suggestion = page.locator(f'button[value="{category}"]')
    try:
        await suggestion.wait_for(state="visible", timeout=UI_TIMEOUT_MS)
    except PlaywrightError:
        raise ActionRefused(
            f"Leboncoin did not suggest « {CATEGORIES[category]} » for this title; "
            "make the title clearer about the kind of rental."
        ) from None

    def is_config(r: Response) -> bool:
        return urlsplit(r.url).path == CONFIG_PATH and r.request.method == "GET"

    async with page.expect_response(is_config, timeout=UI_TIMEOUT_MS * 2) as info:
        await suggestion.click()
    config_response = await info.value
    if config_response.status != 200:
        raise UnexpectedResponse(f"the ad form description: HTTP {config_response.status}")
    return adform.parse_form(await config_response.json())


async def choose_address(page: Page, address: str, expected: str | None = None) -> str:
    """Type the address and pick Leboncoin's first suggestion. Returns its label."""
    box = page.locator("input[name=location]")
    await box.wait_for(state="visible", timeout=UI_TIMEOUT_MS)
    await box.press_sequentially(address, delay=TYPING_DELAY_MS)
    option = page.get_by_role("option").first
    try:
        await option.wait_for(state="visible", timeout=UI_TIMEOUT_MS)
    except PlaywrightError:
        raise ActionRefused(f"Leboncoin found no address for « {address} ».") from None
    label = " ".join((await option.inner_text()).split())
    if expected is not None and label != expected:
        raise ActionRefused(f"Leboncoin now suggests « {label} », not the approved « {expected} ».")
    await option.click()
    await page.wait_for_timeout(STEP_PAUSE_MS)
    if " ".join((await box.input_value()).split()) != label:
        raise ActionRefused("The address field did not take the suggestion.")
    return label


async def _checked(page: Page, key: str) -> set[str]:
    return set(
        await page.evaluate(
            "k => [...document.getElementsByName(k)].filter(i => i.checked).map(i => i.value)", key
        )
    )


def _label_pattern(label: str) -> re.Pattern[str]:
    # Required fields show their label with a trailing « * ».
    return re.compile(rf"^\s*{re.escape(label)}\s*\*?\s*$")


async def fill_field(page: Page, field: Field, value: tuple[str, ...], answers) -> None:
    """Set one field through its widget; a browser error names the field it happened on."""
    try:
        await _fill_field(page, field, value, answers)
    except PlaywrightError as exc:
        raise ActionRefused(
            f"{field.key} ({field.label}, step « {field.step_title} »): {_first_line(exc)}"
        ) from None


async def _fill_field(page: Page, field: Field, value: tuple[str, ...], answers) -> None:
    """Set one field through its widget, then check the page shows exactly that value."""
    key, widget = field.key, field.widget
    choices = field.choices_for(answers) or {}
    if widget.startswith("TEXTFIELD") or widget in ("PRICE", "TEXTAREA"):
        box = page.locator(f'[name="{key}"]').first
        await box.wait_for(state="visible", timeout=UI_TIMEOUT_MS)
        await box.fill("")
        if widget == "TEXTAREA":
            await box.fill(value[0])  # long text: typing it key by key would take minutes
        else:
            await box.press_sequentially(value[0], delay=TYPING_DELAY_MS)
        shown = await box.input_value()
        if widget == "PRICE":
            same = re.sub(r"\D", "", shown) == value[0]
        else:  # e.g. a date typed « 12/2026 » may show as « 12 / 2026 »
            same = re.sub(r"\s", "", shown) == re.sub(r"\s", "", value[0])
        if not same:
            raise ActionRefused(f"{key}: the form shows « {shown} » instead of « {value[0]} ».")
    elif widget.startswith("SELECT_CELLS") or widget == "MULTI_SELECT_CELLS":
        kind = "checkbox" if field.multiple else "radio"
        for v in value:
            if v not in await _checked(page, key):
                await page.locator(f'label[for="{kind}{key}{v}"]').click(timeout=UI_TIMEOUT_MS)
        if await _checked(page, key) != set(value):
            raise ActionRefused(f"{key}: the form did not take {list(value)}.")
    elif widget == "RADIO":
        # The clickable radio is a button next to the hidden input holding the same value.
        button = page.locator(f'input[name="{key}"][value="{value[0]}"]').first.locator(
            f"xpath=ancestor::*[.//button[@role='radio' and @value='{value[0]}']][1]"
            f"//button[@role='radio' and @value='{value[0]}']"
        )
        await button.first.click(timeout=UI_TIMEOUT_MS)
        if await _checked(page, key) != {value[0]}:
            raise ActionRefused(f"{key}: the form did not take {value[0]}.")
    elif widget == "TOGGLE":
        box = page.locator(f'input[name="{key}"]').first
        on = value[0] == "true" if field.answer_type == "BOOL" else choices.get(value[0]) == "Oui"
        if await box.is_checked() != on:
            switch = box.locator("xpath=ancestor::*[.//*[@role='switch']][1]").get_by_role("switch")
            await switch.click(timeout=UI_TIMEOUT_MS)
        if await box.is_checked() != on:
            raise ActionRefused(f"{key}: the switch did not change.")
    elif widget in ("SINGLE_SELECT", "MULTI_SELECT"):
        combo = page.get_by_role("combobox", name=_label_pattern(field.label))
        await combo.click(timeout=UI_TIMEOUT_MS)
        for v in value:
            await page.get_by_role("option", name=choices[v], exact=True).click(timeout=UI_TIMEOUT_MS)
            if field.multiple:
                await page.wait_for_timeout(200)
        if field.multiple:
            await page.keyboard.press("Escape")
            shown = await combo.inner_text()
            missing = [choices[v] for v in value if choices[v] not in shown]
        else:
            shown = await combo.input_value()
            missing = [] if shown == choices[value[0]] else [choices[value[0]]]
        if missing:
            raise ActionRefused(f"{key}: the form did not take {missing}.")
    else:
        raise ActionRefused(f"{key}: the form uses a field type this server cannot fill ({widget}).")
    await page.wait_for_timeout(200)


async def continue_to(page: Page, next_title: str | None) -> None:
    """Click « Continuer » and wait for the next step; report the form's errors if it stays."""
    await page.wait_for_timeout(STEP_PAUSE_MS)
    try:
        await page.get_by_role("button", name="Continuer", exact=True).click(timeout=UI_TIMEOUT_MS)
    except PlaywrightError as exc:
        raise ActionRefused(f"« Continuer » before « {next_title} »: {_first_line(exc)}") from None
    if next_title is None:
        return
    try:
        await page.get_by_role("heading", name=next_title).first.wait_for(
            state="visible", timeout=UI_TIMEOUT_MS
        )
    except PlaywrightError:
        errors = await page.evaluate(
            """() => [...document.querySelectorAll('[role=alert],[id*="helper"],[data-spark-component*="error"]')]
                 .map(e => e.innerText.trim()).filter(Boolean).slice(0, 8)"""
        )
        raise ActionRefused(f"The form did not accept this step: {errors or 'no message'}.") from None


async def upload_photos(page: Page, files: list[Path]) -> None:
    """Hand the files to the page's file input, as if picked in a file dialog."""
    done: list[Response] = []

    def on_response(response: Response) -> None:
        if urlsplit(response.url).path == UPLOAD_PATH and response.request.method == "POST":
            done.append(response)

    payloads = [
        {
            "name": f.name,
            "mimeType": PHOTO_TYPES.get(f.suffix.lower()) or mimetypes.guess_type(f.name)[0],
            "buffer": f.read_bytes(),
        }
        for f in files
    ]
    page.on("response", on_response)
    try:
        await page.locator("input[type=file]").first.set_input_files(payloads)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 30 + 10 * len(files)
        while len(done) < len(files) and loop.time() < deadline:
            await asyncio.sleep(0.5)
    finally:
        page.remove_listener("response", on_response)
    failed = [r.status for r in done if r.status not in (200, 201)]
    if len(done) < len(files) or failed:
        raise ActionRefused(f"Photo upload failed ({len(done)}/{len(files)} answered, errors {failed}).")
    remove = page.get_by_role("button", name=re.compile(r"^Retirer la photo numéro \d+$"))
    if await remove.count() != len(files):
        raise ActionRefused("The form does not show every uploaded photo.")


async def fill_steps(
    page: Page, fields: list[Field], resolved: adform.Resolved, files: list[Path] | None
) -> None:
    """Fill every step after the address, in order. Stops on the contact step."""
    values = {f.key: v for f, v in resolved.fields}
    answers = {f.key: v for f, v in resolved.fields}
    steps: list[tuple[str, str]] = []
    for f in fields:
        if (f.step, f.step_title) not in steps:
            steps.append((f.step, f.step_title))
    for index, (step, _title) in enumerate(steps):
        next_title = steps[index + 1][1] if index + 1 < len(steps) else None
        if step.endswith("LOCATION"):
            continue  # filled by fill_location before this
        if step.endswith("IMAGES"):
            if files:
                await upload_photos(page, files)
        else:
            for f in fields:
                if f.step == step and f.key in values and f.exists(answers):
                    await fill_field(page, f, values[f.key], answers)
        if step.endswith("CONTACT"):
            return  # the caller submits the last step under the network guards
        await continue_to(page, next_title)


async def check_contact(page: Page) -> None:
    """The contact step is pre-filled from the account; refuse to go on if it is not."""
    for key in ("email", "phone"):
        box = page.locator(f'input[name="{key}"]').first
        if not (await box.input_value()).strip():
            raise ActionRefused(f"The account has no {key} on the contact step; add it on Leboncoin.")


async def fill_location_options(page: Page, fields: list[Field], resolved: adform.Resolved) -> None:
    """The address step's other fields, e.g. the immersive view (Street View) switch."""
    answers = {"location": ("address",), **{f.key: v for f, v in resolved.fields}}
    for f, v in resolved.fields:
        if f.step.endswith("LOCATION") and f.exists(answers):
            await fill_field(page, f, v, answers)


def approved_create(category: str, values: dict[str, tuple[str, ...]], address: str, photos: int):
    attributes = {
        k: list(v) for k, v in values.items() if k not in ("subject", "body", "price", "phone_hidden")
    }
    return {
        "category_id": category,
        "subject": values["subject"][0],
        "body": values["body"][0],
        "price": values["price"][0],
        "address": address,
        "photo_count": photos,
        "hide_phone": values["phone_hidden"] == ("true",),
        "attributes": attributes,
    }


# --- the two public operations ----------------------------------------------------------------


async def preview_create(
    page: Page, settings: Settings, category: str, address: str, values: dict[str, Any], photos: list[str]
) -> dict[str, Any]:
    """Check an ad against the live form. Types the title and address; publishes nothing."""
    check_category(category)
    files = photo_files(settings.photos_dir, photos)
    try:
        fields = await open_form(page, settings, values["subject"], category)
        label = await choose_address(page, address)
    except PlaywrightError as exc:
        raise ActionRefused(f"The ad form did not behave as expected ({_first_line(exc)}).") from None
    resolved = adform.resolve(fields, values, {"location": ("address",), "images": ("x",)})
    answers = {f.key: v for f, v in resolved.fields}
    shown = {}
    for f, v in resolved.fields:
        if f.key in ("subject", "body", "price", "phone_hidden"):
            continue
        choices = f.choices_for(answers)
        shown[f.label] = [choices[x] for x in v] if choices else v[0]
    return {
        "category": CATEGORIES[category],
        "title": values["subject"],
        "price": int(values["price"]),
        "address": label,
        "photos": [f.name for f in files],
        "phone_hidden": bool(values["phone_hidden"]),
        "details": shown,
        "description": values["body"],
        "optional_fields_left_empty": [adform.describe(f, answers) for f in resolved.optional],
    }


# Searched, not anchored: the button may hold an icon or hidden text besides its label.
PUBLISH_FREE = re.compile(r"Déposer sans booster|Continuer sans option")


async def _publish_options(page: Page, ad_id: int) -> None:
    """On the options page, go on without buying anything.

    The button is found by its visible text: its accessible name can differ (seen live, where an
    exact role/name lookup missed a button showing « Déposer sans booster mon annonce »).
    """
    button = page.locator("button:visible").filter(has_text=PUBLISH_FREE)
    try:
        await button.first.wait_for(state="visible", timeout=UI_TIMEOUT_MS)
    except PlaywrightError:
        names = await page.evaluate(
            "() => [...document.querySelectorAll('button')].map(b => b.innerText.trim()).filter(Boolean)"
        )
        raise ActionUnconfirmed(
            f"Ad {ad_id} was created but not submitted: no known button on the options page "
            f"({names[-12:]}). Finish it in noVNC (no paid option needed)."
        ) from None
    await button.first.click(timeout=UI_TIMEOUT_MS)


CONFIRMATION = re.compile(r"Nous avons bien re[çc]u votre annonce")


async def _check_confirmation(page: Page, ad_id: int) -> None:
    """Success only once the page says « Nous avons bien reçu votre annonce ! »."""
    try:
        await page.wait_for_url(re.compile(r"/deposer-une-annonce/confirmation"), timeout=UI_TIMEOUT_MS * 2)
        await page.get_by_text(CONFIRMATION).first.wait_for(state="visible", timeout=UI_TIMEOUT_MS)
    except PlaywrightError:
        raise ActionUnconfirmed(
            f"Ad {ad_id} was created and submitted, but Leboncoin never showed « Nous avons bien reçu "
            "votre annonce ! ». Check list_ads (and noVNC) before trying again."
        ) from None


async def create_ad(
    page: Page,
    settings: Settings,
    category: str,
    address: str,
    approved_address: str,
    values: dict[str, Any],
    photos: list[str],
) -> dict[str, Any]:
    check_category(category)
    files = photo_files(settings.photos_dir, photos)
    try:
        fields = await open_form(page, settings, values["subject"], category)
        await choose_address(page, address, expected=approved_address)
    except PlaywrightError as exc:
        raise ActionRefused(f"The ad form did not behave as expected ({_first_line(exc)}).") from None
    resolved = adform.resolve(fields, values, {"location": ("address",), "images": ("x",)})
    raw = {f.key: v for f, v in resolved.fields}
    location_step = next(f for f in fields if f.key == "location")
    next_titles = [f.step_title for f in fields if f.step != location_step.step]
    try:
        await fill_location_options(page, fields, resolved)
        await continue_to(page, next_titles[0] if next_titles else None)
        await fill_steps(page, fields, resolved, files)
        await check_contact(page)
    except PlaywrightError as exc:
        # Still before the last step: nothing was sent to create the ad.
        raise ActionRefused(
            f"The form could not be filled ({_first_line(exc)}); nothing was published."
        ) from None

    create_guard = CreateGuard(approved_create(category, raw, approved_address, len(files)))
    submit_guard = SubmitGuard()
    await page.route(CreateGuard.PATTERN, create_guard.handle)
    await page.route(SubmitGuard.PATTERN, submit_guard.handle)
    try:
        def is_create(r: Response) -> bool:
            return urlsplit(r.url).path == CREATE_PATH and r.request.method == "POST"

        async with page.expect_response(is_create, timeout=UI_TIMEOUT_MS * 3) as info:
            await continue_to(page, None)
            try:
                refusal = await asyncio.wait_for(asyncio.shield(create_guard.decision), 30)
            except TimeoutError:
                raise ActionRefused(
                    "The last step sent no create request; nothing was created."
                ) from None
            if refusal:
                raise ActionRefused(f"Blocked before reaching Leboncoin: {refusal}. Nothing was created.")
        response = await info.value
        if response.status not in (200, 201):
            raise ActionUnconfirmed(f"Leboncoin answered HTTP {response.status} to the new ad.")
        created = await response.json()
        ad_id = created.get("ad_id")
        if not isinstance(ad_id, int):
            raise ActionUnconfirmed(f"Leboncoin did not return the new ad's id ({created}).")
        submit_guard.ad_id = ad_id

        await page.wait_for_url(re.compile(r"/deposer-une-annonce/options"), timeout=UI_TIMEOUT_MS * 2)
        await page.wait_for_timeout(STEP_PAUSE_MS * 2)
        await _publish_options(page, ad_id)
        try:
            refusal = await asyncio.wait_for(asyncio.shield(submit_guard.decision), 30)
        except TimeoutError:
            raise ActionUnconfirmed(
                f"Ad {ad_id} was created but not submitted; finish it in noVNC (no paid option)."
            ) from None
        if refusal:
            raise ActionUnconfirmed(
                f"Ad {ad_id} was created, but its submit was blocked: {refusal}. "
                "Finish it in noVNC without any paid option."
            )
        await _check_confirmation(page, ad_id)
    except PlaywrightError as exc:
        raise ActionUnconfirmed(f"The form stopped unexpectedly ({_first_line(exc)}); check list_ads.") from None
    finally:
        await page.unroute(CreateGuard.PATTERN, create_guard.handle)
        await page.unroute(SubmitGuard.PATTERN, submit_guard.handle)
    return {"created": True, "ad_id": str(ad_id), "status": "submitted for Leboncoin's review"}
