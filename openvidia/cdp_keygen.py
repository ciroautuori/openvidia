"""
CDP-based NVIDIA API key generation.

Connects to a running Chrome instance via Chrome DevTools Protocol,
finds authenticated build.nvidia.com tabs, and generates fresh API keys
without cookies or password automation — just CDP + existing browser session.

Flow:
  1. Read DevToolsActivePort → WebSocket URL
  2. Target.getTargets → filter type=page, url=build.nvidia.com/settings/api-keys
  3. Attach to target (flatten=True)
  4. Check auth: "Sign In" not in document.body.innerText
  5. If authenticated:
     a. Click "Generate API Key" → opens modal form
     b. Optionally fill key name
     c. Click "Generate Key" → calls NVIDIA API
     d. Wait for "API Key Granted" dialog
     e. Extract nvapi-... from <input value="nvapi-...">
     f. Save key
     g. Close modal with "Close Modal" button
"""

import asyncio
import itertools
import json
import logging
import re
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_CDP_PATHS = [
    Path.home() / ".config" / "google-chrome-cdp" / "DevToolsActivePort",
    Path.home() / ".config" / "google-chrome" / "DevToolsActivePort",
]

_msgi = itertools.count(1000)

# ── Low-level CDP helpers ──────────────────────────────────────────

_UUID_RE = re.compile(
    r"/devtools/browser/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
)


def read_ws_url() -> str | None:
    """Read Chrome DevToolsActivePort and build WebSocket URL.

    Tries multiple paths:
      1. ~/.config/google-chrome-cdp/DevToolsActivePort  (CDP profile)
      2. ~/.config/google-chrome/DevToolsActivePort       (default profile)

    File format (Chrome ~150):
      line 1: port number
      line 2: /devtools/browser/<UUID> possibly followed by PID (no separator)
    """
    for p in _CDP_PATHS:
        try:
            text = p.read_text().strip()
            parts = text.split("\n")
            port = parts[0].strip()
            if len(parts) > 1:
                m = _UUID_RE.search(parts[1])
                if m:
                    return f"ws://127.0.0.1:{port}/devtools/browser/{m.group(1)}"
            return None
        except (FileNotFoundError, IndexError, OSError):
            continue
    logger.warning(
        "Cannot read DevToolsActivePort (tried %s)",
        ", ".join(str(p) for p in _CDP_PATHS),
    )
    return None


async def _recv(ws, timeout: float = 10) -> dict:
    return json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))


async def _send(ws, msg: dict):
    await ws.send(json.dumps(msg))


async def _eval(ws, sid: str, expr: str, msg_id: int = 100, timeout_sec: float = 10) -> dict | None:
    """Runtime.evaluate. Returns the ``result`` dict or None."""
    await _send(
        ws,
        {
            "id": msg_id,
            "sessionId": sid,
            "method": "Runtime.evaluate",
            "params": {
                "expression": expr,
                "returnByValue": True,
                "awaitPromise": True,
                "timeout": int(timeout_sec * 1000),
            },
        },
    )
    deadline = time.time() + timeout_sec + 2
    while time.time() < deadline:
        try:
            msg = await _recv(ws, timeout=3)
        except TimeoutError:
            return None
        if msg.get("id") == msg_id:
            result = msg.get("result", {}).get("result", {})
            if result.get("subtype") == "error":
                logger.debug("JS error: %s", result.get("description", "")[:200])
                return None
            return result
    return None


async def _eval_val(ws, sid: str, expr: str, msg_id: int = 100, timeout_sec: float = 10) -> any:
    r = await _eval(ws, sid, expr, msg_id=msg_id, timeout_sec=timeout_sec)
    return r.get("value") if r else None


async def _js_click(ws, sid: str, button_text: str, msg_id: int = 300) -> bool:
    """Click a button by exact text content. Returns True if clicked."""
    val = await _eval_val(
        ws,
        sid,
        f"""
    (() => {{
        const btn = Array.from(document.querySelectorAll('button')).find(b =>
            b.textContent.trim() === {json.dumps(button_text)}
        );
        if (!btn) return 'NOT_FOUND';
        btn.click();
        return 'CLICKED';
    }})();
    """,
        msg_id=msg_id,
    )
    return val == "CLICKED"


# ── Target management ──────────────────────────────────────────────


async def list_targets(ws_url: str, retries: int = 3) -> list:
    """Return all page targets via Target.getTargets, with retries."""
    import websockets

    for attempt in range(retries):
        try:
            async with websockets.connect(ws_url, max_size=2**24, open_timeout=10) as ws:
                await _send(ws, {"id": 1, "method": "Target.getTargets"})
                resp = await _recv(ws)
                return resp.get("result", {}).get("targetInfos", [])
        except (TimeoutError, OSError, websockets.InvalidStatus) as e:
            if attempt < retries - 1:
                logger.warning(
                    "CDP connect attempt %d/%d failed: %s — retrying",
                    attempt + 1,
                    retries,
                    e,
                )
                await asyncio.sleep(1)
            else:
                raise
    return []


async def attach_and_get_sid(ws, target_id: str, timeout: float = 8) -> str | None:
    """
    Attach to a page target with flatten=True from an already-open connection.
    Returns sessionId.
    """
    await _send(
        ws,
        {
            "id": 888,
            "method": "Target.attachToTarget",
            "params": {"targetId": target_id, "flatten": True},
        },
    )
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            msg = await _recv(ws, timeout=3)
        except TimeoutError:
            break
        if msg.get("method") == "Target.attachedToTarget":
            return msg.get("params", {}).get("sessionId", "")
        if msg.get("method") == "Target.targetInfoChanged":
            continue
        if msg.get("id") == 888:
            return msg.get("result", {}).get("sessionId", "")
    return None


# ── Auth check ─────────────────────────────────────────────────────


async def is_authenticated(ws, sid: str) -> bool:
    """Page shows authenticated API keys content (not the Sign In page)."""
    val = await _eval_val(
        ws,
        sid,
        """
        document.body.innerText.includes('Sign In')
        && !document.body.innerText.includes('API Keys')
        ? 'NOT_AUTH' : 'AUTH'
    """,
        msg_id=900,
    )
    return val == "AUTH"


# ── Key generation ─────────────────────────────────────────────────


async def generate_one_key(ws, sid: str, ws_url: str, key_name: str = "") -> str | None:
    """
    Generate one API key on an already-attached, authenticated page.
    Uses the same WebSocket connection throughout (sid stays valid).

    Steps: click "Generate API Key" → fill name (optional) →
    try "Generate Key" if modal asks for it (NVIDIA now auto-generates
    immediately on some accounts) → extract key from input → close modal.
    """
    # ── 1. Click "Generate API Key" ──────────────────────────────────
    ok = await _js_click(ws, sid, "Generate API Key", msg_id=next(_msgi))
    if not ok:
        logger.warning("Button 'Generate API Key' not found")
        return None
    await asyncio.sleep(2)

    # ── 2. Fill key name if provided ─────────────────────────────────
    if key_name:
        await _eval_val(
            ws,
            sid,
            f"""
        (() => {{
            const input = document.querySelector('.nv-modal-overlay input[type="text"]');
            if (!input) return 'NO_INPUT';
            const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
            setter.call(input, {json.dumps(key_name)});
            input.dispatchEvent(new Event('input', {{bubbles: true}}));
            input.dispatchEvent(new Event('change', {{bubbles: true}}));
            return 'FILLED';
        }})();
        """,
            msg_id=next(_msgi),
        )

    # ── 3. Extract key (two sub-flows) ──────────────────────────────
    # NVIDIA's current UI auto-generates the key immediately inside a modal
    # input on some accounts; on others it shows a "Generate Key" button
    # first.  Handle both: poll for nvapi- in ANY input, then try clicking
    # the footer button if nothing found.
    deadline = time.time() + 15
    while time.time() < deadline:
        val = await _eval_val(
            ws,
            sid,
            """
        (() => {
            const all = document.querySelectorAll('input, textarea');
            for (const el of all) {
                const v = el.value || '';
                if (v.startsWith('nvapi-')) return v;
            }
            const text = document.body.innerText;
            const m = text.match(/nvapi-[A-Za-z0-9_-]{40,}/);
            return m ? m[0] : null;
        })();
        """,
            msg_id=next(_msgi),
        )
        if val and str(val).startswith("nvapi-"):
            key = str(val)
            await _js_click(ws, sid, "Close Modal", msg_id=next(_msgi))
            await asyncio.sleep(0.3)
            return key
        await asyncio.sleep(0.5)

        # ── 3b. If no key yet, try "Generate Key" once ────────────────
        ok = await _js_click(ws, sid, "Generate Key", msg_id=next(_msgi))
        if ok:
            await asyncio.sleep(1.5)
            break  # fall through to post-click polling below

    # ── 4. Post-Generate-Key poll (only reached if step 3b fired) ────
    deadline = time.time() + 15
    while time.time() < deadline:
        val = await _eval_val(
            ws,
            sid,
            """
        (() => {
            const all = document.querySelectorAll('input, textarea');
            for (const el of all) {
                const v = el.value || '';
                if (v.startsWith('nvapi-')) return v;
            }
            const text = document.body.innerText;
            const m = text.match(/nvapi-[A-Za-z0-9_-]{40,}/);
            return m ? m[0] : null;
        })();
        """,
            msg_id=next(_msgi),
        )
        if val and str(val).startswith("nvapi-"):
            key = str(val)
            await _js_click(ws, sid, "Close Modal", msg_id=next(_msgi))
            await asyncio.sleep(0.3)
            return key
        await asyncio.sleep(0.5)

    logger.warning("Key not detected within 30s timeout")
    return None


# ── Multi-account ──────────────────────────────────────────────────


async def generate_for_all(
    ws_url: str,
    accounts: list[dict],
    max_per_account: int = 1,
    timeout_per_context: float = 25,
) -> list[tuple[str, str, bool]]:
    """
    Iterate all authenticated build.nvidia.com tabs across all browser
    contexts and generate keys. Uses a single CDP connection per context.

    Returns [(account_name, key_or_error, success)].
    """

    targets = await list_targets(ws_url)
    nv_pages = [
        t for t in targets if t.get("type") == "page" and "build.nvidia.com" in t.get("url", "")
    ]

    if not nv_pages:
        return [(a["name"], "No build.nvidia.com tab open", False) for a in accounts]

    # Group by browserContextId (one per Chrome profile)
    contexts: dict = {}
    for t in nv_pages:
        ctx = t.get("browserContextId", "")
        contexts.setdefault(ctx, []).append(t)

    logger.info("Found %d context(s) with build.nvidia.com tabs", len(contexts))
    results: list = []

    for ctx_id, ctx_targets in contexts.items():
        target = ctx_targets[0]
        tid = target["targetId"]

        try:
            result = await asyncio.wait_for(
                _process_context(ws_url, ctx_id, tid, accounts, max_per_account),
                timeout=timeout_per_context,
            )
            results.extend(result)
        except TimeoutError:
            results.append((f"ctx_{ctx_id[:8]}", "Timed out", False))

    return results


async def _process_context(
    ws_url: str, ctx_id: str, tid: str, accounts: list, max_per_account: int
) -> list:
    """Process a single browser context: attach, auth check, generate keys."""
    import websockets

    results = []
    async with websockets.connect(ws_url, max_size=2**24, open_timeout=10) as ws:
        sid = await attach_and_get_sid(ws, tid)
        if not sid:
            return [(f"ctx_{ctx_id[:8]}", "Cannot attach", False)]

        if not await is_authenticated(ws, sid):
            return [(f"ctx_{ctx_id[:8]}", "Not authenticated", False)]

        page_name = (
            await _eval_val(
                ws,
                sid,
                r"""
            (() => {
                const m = document.body.innerText.match(/within ([^\n]+)/);
                return m ? m[1].trim() : '';
            })();
        """,
                msg_id=401,
            )
        ) or ""

        matched = [a for a in accounts if page_name.lower() in a["name"].lower()]
        if not matched:
            matched = accounts

        for acct in matched:
            for _ in range(max_per_account):
                key = await generate_one_key(ws, sid, ws_url)
                if key:
                    results.append((acct["name"], key, True))
                else:
                    results.append((acct["name"], "Generation failed", False))
                    break

    # Report accounts with no match
    handled = {r[0] for r in results}
    for a in accounts:
        if a["name"] not in handled:
            results.append((a["name"], "No authenticated session", False))

    return results


# ── Persistence ────────────────────────────────────────────────────


def save_key(key: str, account_name: str, accounts_path: Path, keys_path: Path):
    """Persist key to keys.json and link it in accounts.json."""
    from . import config

    keys = config.load_saved_keys_file()
    if key not in keys:
        keys.append(key)
        config.save_keys_file(keys)

    try:
        accts_data = json.loads(accounts_path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        accts_data = []

    for acct in accts_data:
        if acct["name"] == account_name:
            if key not in acct.get("keys", []):
                acct.setdefault("keys", []).append(key)
            break

    accounts_path.write_text(json.dumps(accts_data, indent=2))
    logger.info("Saved key %s… to account %s", key[:12], account_name)
