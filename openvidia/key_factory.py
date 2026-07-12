"""
Playwright automation for NVIDIA API key lifecycle on build.nvidia.com.

Two authentication modes:
  1. ``login_generate_key(email, password)`` — full login flow.
  2. ``generate_key(cookie_json)`` — cookie-based (legacy).

Both navigate to the API key settings page, generate a fresh ``nvapi-`` key,
and optionally delete an old one. Runs headless Chromium via Playwright.
"""
import json
import logging
import time
from typing import Any, Dict, List, Optional

from playwright.sync_api import sync_playwright, Page, BrowserContext

logger = logging.getLogger(__name__)

BASE_URL = "https://build.nvidia.com"
API_KEYS_URL = f"{BASE_URL}/settings/api-keys"
TIMEOUT = 30_000  # ms


# ── Cookie helpers (legacy) ──────────────────────────────────────────

def _cookies_from_json(raw: str) -> List[Dict[str, Any]]:
    data = json.loads(raw)
    if isinstance(data, dict):
        data = [data]
    mapping = {
        "no_restriction": "None", "lax": "Lax", "strict": "Strict",
        "none": "None", "Lax": "Lax", "Strict": "Strict", "": "Lax",
    }
    out = []
    for c in data:
        ck = {
            "name": c["name"],
            "value": c["value"],
            "domain": c.get("domain", "").lstrip("."),
            "path": c.get("path", "/"),
            "secure": c.get("secure", False),
            "httpOnly": c.get("httpOnly", False),
            "sameSite": mapping.get(c.get("sameSite") or "Lax", "Lax"),
        }
        if c.get("expirationDate"):
            ck["expires"] = int(c["expirationDate"])
        out.append(ck)
    return out


# ── Browser factory ─────────────────────────────────────────────────

def _browser():
    p = sync_playwright().start()
    b = p.chromium.launch(headless=True)
    ctx = b.new_context(
        user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/145.0.0.0 Safari/537.36",
        viewport={"width": 1280, "height": 800},
    )
    return p, b, ctx


def _browser_chrome_channel(user_data_dir: Optional[str] = None):
    """
    Launch Playwright connected to system Chrome with an optional isolated profile.

    If *user_data_dir* is provided, launches Chrome with that directory as
    ``--user-data-dir``, creating a persistent isolated profile for that
    account. Subsequent runs reuse the same session (login persists).

    Without *user_data_dir*, connects to the user's default Chrome profile
    (``channel="chrome"``).
    """
    p = sync_playwright().start()
    args = ["--new-window"]
    if user_data_dir:
        b = p.chromium.launch_persistent_context(
            user_data_dir=user_data_dir,
            headless=False,
            args=args,
            viewport={"width": 1280, "height": 800},
        )
        ctx = b
        browser = None
    else:
        b = p.chromium.launch(
            headless=False,
            channel="chrome",
            args=args,
        )
        ctx = b.new_context(
            viewport={"width": 1280, "height": 800},
        )
        browser = b
    return p, browser, ctx


def _page(ctx: BrowserContext) -> Page:
    page = ctx.new_page()
    page.set_default_timeout(TIMEOUT)
    return page


# ── Login flow ──────────────────────────────────────────────────────

def login_generate_key(email: str, password: str, old_key: Optional[str] = None) -> str:
    """
    Launch headless Chromium, log into build.nvidia.com with *email* /
    *password*, navigate to API keys, generate a fresh key.

    If *old_key* is given, attempt to delete it first.

    Returns the ``nvapi-...`` key string. Raises ``RuntimeError`` on failure.
    """
    pw, browser, ctx = _browser()
    page = _page(ctx)
    new_key: Optional[str] = None

    try:
        _login(page, email, password)
        new_key = _do_generate(page)
        if old_key:
            _do_delete(page, old_key)
    except RuntimeError:
        raise
    except Exception as e:
        raise RuntimeError(f"Key generation failed: {e}") from e
    finally:
        ctx.close()
        browser.close()
        pw.stop()

    if not new_key:
        raise RuntimeError("Key generation returned empty key")
    return new_key


def _login(page: Page, email: str, password: str):
    """Full login flow on build.nvidia.com."""
    page.goto(BASE_URL, wait_until="networkidle", timeout=TIMEOUT)
    time.sleep(1)

    # --- Accept cookie banner if present ---
    accept = page.locator("button:has-text('Accept All')")
    if accept.is_visible(timeout=3000):
        accept.click()
        time.sleep(0.5)

    # --- Click Login ---
    login_btn = page.locator(
        "a:has-text('Login'), "
        "button:has-text('Login'), "
        "a:has-text('Sign In'), "
        "button:has-text('Sign In')"
    ).first
    login_btn.wait_for(state="visible", timeout=10000)
    login_btn.click()
    time.sleep(1)

    # --- Email step ---
    _fill_and_next(page, email)

    # --- Password step ---
    _fill_and_next(page, password)

    # Wait for navigation back to dashboard
    page.wait_for_url(lambda u: "login" not in u.lower(), timeout=TIMEOUT)
    time.sleep(1)

    # --- Navigate to API keys ---
    page.goto(API_KEYS_URL, wait_until="networkidle", timeout=TIMEOUT)
    if "login" in page.url.lower() or "signin" in page.url.lower():
        raise RuntimeError("Login failed — still on login page after credentials")

    # Accept cookie banner again on the settings page
    accept2 = page.locator("button:has-text('Accept All')")
    if accept2.is_visible(timeout=3000):
        accept2.click()
        time.sleep(0.5)

    logger.info(f"Logged in as {email}, on API keys page")


def _fill_and_next(page: Page, value: str):
    """Fill the visible text/email input then click the visible submit button."""
    input_el = page.locator(
        "input[type='email'], "
        "input[type='text'], "
        "input[type='password'], "
        "input:not([type='hidden']):not([type='checkbox'])"
    ).first
    input_el.wait_for(state="visible", timeout=8000)
    input_el.fill(value)
    time.sleep(0.3)

    next_btn = page.locator(
        "button:has-text('Next'), "
        "button:has-text('Sign In'), "
        "button:has-text('Continue'), "
        "button[type='submit']"
    ).first
    next_btn.wait_for(state="visible", timeout=5000)
    next_btn.click()
    time.sleep(1.5)


# ── Chrome channel (system Chrome session) ─────────────────────────

def chrome_channel_generate_key(
    old_key: Optional[str] = None,
    user_data_dir: Optional[str] = None,
) -> str:
    """
    Generate API key using system Chrome with an optional isolated profile.

    If *user_data_dir* is provided, launches Chrome with an isolated profile
    at that path — the user logs in once and the session persists across runs.
    Without it, reuses the default Chrome profile.

    Returns the ``nvapi-...`` key string. Raises ``RuntimeError`` on failure.
    """
    pw, browser, ctx = _browser_chrome_channel(user_data_dir)
    page = _page(ctx)
    new_key: Optional[str] = None
    is_persistent = user_data_dir is not None

    try:
        page.goto(API_KEYS_URL, wait_until="networkidle", timeout=TIMEOUT)
        if "login" in page.url.lower() or "signin" in page.url.lower():
            raise RuntimeError("Not logged into build.nvidia.com in Chrome")

        accept = page.locator("button:has-text('Accept All')")
        if accept.is_visible(timeout=2000):
            accept.click()
            page.wait_for_timeout(1000)

        new_key = _do_generate(page)
        if old_key:
            _do_delete(page, old_key)
    except RuntimeError:
        raise
    except Exception as e:
        raise RuntimeError(f"Chrome channel key generation failed: {e}") from e
    finally:
        if is_persistent:
            ctx.close()   # launch_persistent_context returns context directly
        else:
            ctx.close()
            if browser:
                browser.close()
        pw.stop()

    if not new_key:
        raise RuntimeError("Chrome channel returned empty key")
    return new_key


# ── Legacy cookie auth ──────────────────────────────────────────────

def generate_key(cookie_json: str, old_key: Optional[str] = None,
                 email: str = "", password: str = "") -> str:
    """Generate API key — cookies first, fallback to email+password login.

    If *cookie_json* is provided and valid, uses cookie injection.
    If cookies expire (redirected to login) and *email*+*password* given,
    falls back to full login flow.
    Returns the ``nvapi-...`` key string.
    """
    cookies = _cookies_from_json(cookie_json) if cookie_json else []
    pw, browser, ctx = _browser()
    page = _page(ctx)
    new_key: Optional[str] = None

    try:
        if cookies:
            ctx.add_cookies(cookies)
        page.goto(API_KEYS_URL, wait_until="networkidle", timeout=TIMEOUT)

        if "login" in page.url.lower() or "signin" in page.url.lower():
            if email and password:
                logger.info("Cookies expired, logging in with %s...", email)
                page.goto(BASE_URL, wait_until="networkidle", timeout=TIMEOUT)
                _login(page, email, password)
                page.goto(API_KEYS_URL, wait_until="networkidle", timeout=TIMEOUT)
            else:
                raise RuntimeError(
                    "Cookies expired. Either refresh cookies by logging in manually "
                    "or save the password: ov accounts add <name> -e <email> -p <password>"
                )

        # Accept cookie banner if present
        accept = page.locator("button:has-text('Accept All')")
        if accept.is_visible(timeout=2000):
            accept.click()
            page.wait_for_timeout(1000)

        new_key = _do_generate(page)
        if old_key:
            _do_delete(page, old_key)
    except RuntimeError:
        raise
    except Exception as e:
        raise RuntimeError(f"Key generation failed: {e}") from e
    finally:
        ctx.close()
        browser.close()
        pw.stop()

    if not new_key:
        raise RuntimeError("Key generation returned empty key")
    return new_key


# ── Shared key generation / deletion ─────────────────────────────────

def _do_generate(page: Page) -> str:
    """Click 'Generate API Key' and extract the new key value."""
    gen_btn = page.locator(
        "button:has-text('Generate API Key'), "
        "button:has-text('Generate Key'), "
        "[data-testid='generate-api-key'], "
        "button:has-text('Create Key'), "
        "button:has-text('New Key')"
    ).first
    try:
        gen_btn.wait_for(state="visible", timeout=10000)
    except Exception:
        # Page might need a refresh
        page.reload(wait_until="networkidle")
        page.wait_for_timeout(2000)
        gen_btn.wait_for(state="visible", timeout=TIMEOUT)
    time.sleep(0.5)
    gen_btn.click()
    page.wait_for_timeout(2000)

    key_input = page.locator(
        "input[readonly][value*='nvapi-'], "
        "textarea[readonly]:has-text('nvapi-'), "
        "[data-testid='api-key-value'] input, "
        ".api-key-display input, "
        "[class*='keyDisplay'] input, "
        "[class*='keyValue'] input, "
        "code:has-text('nvapi-'), "
        "pre:has-text('nvapi-')"
    ).first
    try:
        key_input.wait_for(state="visible", timeout=TIMEOUT)
    except Exception:
        key_text = page.locator(
            "[class*='keyValue'], [class*='key-display'], "
            "[data-testid*='key-value'], text=nvapi-"
        ).first
        key_text.wait_for(state="visible", timeout=5000)
        raw = key_text.text_content() or ""
    else:
        tag = key_input.evaluate("el => el.tagName.toLowerCase()")
        raw = key_input.input_value() if tag == "input" or tag == "textarea" else (key_input.text_content() or "")

    key = raw.strip().split("\n")[0].strip()
    if not key.startswith("nvapi-"):
        raise RuntimeError(f"Extracted value does not look like a key: {key!r}")

    close_btn = page.locator(
        "button:has-text('Close'), button:has-text('Done'), "
        "button:has-text('Copy'), [aria-label='Close']"
    ).first
    if close_btn.is_visible():
        close_btn.click()
        time.sleep(0.3)

    return key


def _do_delete(page: Page, old_key: str) -> bool:
    """Try to delete *old_key* from the account's key list."""
    suffix = old_key[-6:].lower()
    del_btn = page.locator(
        f"button:has-text('Delete'):right-of(:text('{suffix}')), "
        f"[data-key*='{suffix}'] button:has-text('Delete'), "
        f"tr:has-text('{suffix}') button:has-text('Delete'), "
        "button:has-text('Delete')"
    ).first
    try:
        del_btn.wait_for(state="visible", timeout=8000)
    except Exception:
        logger.warning("Delete button for old key not found, skipping deletion")
        return False

    del_btn.click()
    time.sleep(0.5)

    confirm = page.locator(
        "button:has-text('Confirm'), button:has-text('Yes'), "
        "button:has-text('Delete'):visible"
    ).first
    try:
        confirm.wait_for(state="visible", timeout=5000)
        confirm.click()
        time.sleep(1)
    except Exception:
        pass

    return True
