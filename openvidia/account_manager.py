"""
Account Manager — ties together NVIDIA accounts and the proxy key pool.

Each account tracks which API keys belong to it. When the proxy reports a key
as failed (401/403/404), the account manager generates a fresh key:

  1. Primary: CDP-based generation via an already-authenticated Chrome tab
     (works with Google OAuth accounts — no password/cookie hacks).
  2. Fallback: Playwright with email+password (accounts without OAuth).

The new key is swapped into the proxy pool transparently.
"""

import asyncio
import json
import logging
import threading
import time
from collections.abc import Callable
from pathlib import Path

from . import config
from .proxy_state import ProxyState

logger = logging.getLogger(__name__)


class Account:
    """A single NVIDIA account capable of generating API keys."""

    def __init__(
        self,
        name: str,
        email: str = "",
        password: str = "",
        cookie_json: str = "",
        keys: list[str] | None = None,
    ):
        self.name = name
        self.email = email
        self.password = password
        self.cookie_json = cookie_json
        self.keys: list[str] = list(keys) if keys else []
        self._gen_lock = threading.Lock()

    @property
    def has_credentials(self) -> bool:
        return bool(self.email and self.password)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "email": self.email,
            "password": self.password,
            "cookie_json": self.cookie_json,
            "keys": list(self.keys),
        }

    @staticmethod
    def from_dict(d: dict) -> "Account":
        return Account(
            name=d["name"],
            email=d.get("email", ""),
            password=d.get("password", ""),
            cookie_json=d.get("cookie_json", ""),
            keys=d.get("keys", []),
        )

    def __repr__(self):
        auth = "🔑" if self.has_credentials else "🍪"
        return f"<Account {self.name!r} {auth} keys={len(self.keys)}>"


class AccountManager:
    """
    Manages the lifecycle of accounts and their API keys.

    Hooks into ProxyState via ``on_key_failed`` callback. When a key dies,
    finds its owning account, generates a replacement, and swaps it in.
    """

    def __init__(self, state: ProxyState, accounts_path: Path):
        self.state = state
        self.accounts_path = accounts_path
        self.accounts: list[Account] = []
        # key_value -> account index
        self._key_owner: dict[str, int] = {}
        # Set of keys currently being replenished (prevent double-spawn)
        self._replenishing: set[str] = set()
        self._replenish_lock = threading.Lock()
        # Cross-thread lock for state.keys mutations (asyncio.Lock in proxy is
        # single-thread only — replenish runs from thread pool workers)
        self._state_keys_lock = threading.Lock()
        self._log_cb: Callable[[str], None] = lambda msg: logger.info(msg)

    def set_log_cb(self, cb: Callable[[str], None]):
        self._log_cb = cb

    # ── loading / saving ──────────────────────────────────────────────

    def load(self):
        """Load accounts from disk and rebuild the key→owner map."""
        try:
            raw = self.accounts_path.read_text()
            data = json.loads(raw)
            self.accounts = [Account.from_dict(a) for a in data]
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            self.accounts = []
        self._rebuild_map()
        self._sync_keys_to_state()

    def save(self):
        """Persist accounts to disk."""
        from .config import atomic_write

        data = [a.to_dict() for a in self.accounts]
        atomic_write(self.accounts_path, json.dumps(data, indent=2))

    def _rebuild_map(self):
        self._key_owner = {}
        for idx, acct in enumerate(self.accounts):
            for k in acct.keys:
                self._key_owner[k] = idx

    def _sync_keys_to_state(self):
        """Ensure the proxy's key list matches all account keys."""
        all_keys = []
        seen: set[str] = set()
        for acct in self.accounts:
            for k in acct.keys:
                if k not in seen:
                    seen.add(k)
                    all_keys.append(k)
        # Preserve order — keep working keys, append new ones
        existing = set(all_keys)
        with self._state_keys_lock:
            for k in self.state.keys:
                if k not in existing and k not in seen:
                    all_keys.append(k)
            self.state.keys[:] = all_keys

    # ── account CRUD ──────────────────────────────────────────────────

    def add_account(
        self, name: str, email: str = "", password: str = "", cookie_json: str = ""
    ) -> Account:
        if any(a.name == name for a in self.accounts):
            raise ValueError(f"Account {name!r} already exists")
        acct = Account(name=name, email=email, password=password, cookie_json=cookie_json)
        self.accounts.append(acct)
        self.save()
        auth = "🔑" if acct.has_credentials else "🍪"
        self._log_cb(f"➕ Account {name!r} added ({auth})")
        return acct

    def remove_account(self, name: str):
        acct = self._find(name)
        if acct is None:
            raise ValueError(f"Account {name!r} not found")
        for k in acct.keys:
            if k in self.state.keys:
                self.state.keys.remove(k)
        self.accounts.remove(acct)
        self._rebuild_map()
        self.save()
        self._log_cb(f"➖ Account {name!r} removed ({len(acct.keys)} keys)")

    def update_account(self, name: str, email: str = "", password: str = "", cookie_json: str = ""):
        acct = self._find(name)
        if acct is None:
            raise ValueError(f"Account {name!r} not found")
        if email:
            acct.email = email
        if password:
            acct.password = password
        if cookie_json:
            acct.cookie_json = cookie_json
        self.save()
        self._log_cb(f"✏️ Account {name!r} updated")

    def get_accounts_info(self) -> list[dict]:
        return [
            {
                "name": a.name,
                "key_count": len(a.keys),
                "auth_type": "🔑 email"
                if a.has_credentials
                else ("🍪 cookies" if a.cookie_json else "⚠ none"),
                "email": a.email if a.has_credentials else "",
                "cookies_preview": a.cookie_json[:80] if a.cookie_json else "",
                "has_credentials": a.has_credentials,
            }
            for a in self.accounts
        ]

    # ── key replenishment ─────────────────────────────────────────────

    def on_key_failed(self, key: str):
        """
        Called from proxy hot-path (sync, inside FastAPI async handler).

        Since we're already on the event-loop thread, schedule the
        replenish coroutine directly via ``create_task``.  Never blocks
        the caller — returns immediately.
        """
        with self._replenish_lock:
            if key in self._replenishing:
                return
            self._replenishing.add(key)

        idx = self._key_owner.get(key)
        if idx is None:
            self._log_cb(f"⚠ Key {key[:12]}… is not owned by any account — skipping")
            with self._replenish_lock:
                self._replenishing.discard(key)
            return

        self._log_cb(f"🔄 Replenish key {key[:12]}… (account {self.accounts[idx].name})")

        try:
            asyncio.create_task(self._do_replenish(key, idx))
        except RuntimeError:
            # No running loop — fallback to thread
            threading.Thread(target=self._do_replenish_sync, args=(key, idx), daemon=True).start()

    async def _do_replenish(self, old_key: str, acct_idx: int):
        try:
            acct = self.accounts[acct_idx]
            new_key = await self._generate_and_swap_cdp(acct.name, old_key, acct_idx)

            # Fallback: Playwright with email+password
            if not new_key and acct.has_credentials:
                self._log_cb(f"  CDP failed — trying Playwright for {acct.name}")
                new_key = await asyncio.to_thread(self._generate_and_swap_pw, old_key, acct_idx)
            elif not new_key and acct.cookie_json:
                self._log_cb(f"  CDP failed — trying cookie auth for {acct.name}")
                new_key = await asyncio.to_thread(self._generate_and_swap_pw, old_key, acct_idx)

            if new_key:
                self._log_cb(f"✅ Replenished: {old_key[:12]}… → {new_key[:12]}…")
            else:
                self._log_cb(f"✗ Replenish returned no key for {acct.name}")
        except Exception as e:
            self._log_cb(f"❌ Replenish failed for {old_key[:12]}…: {e}")
        finally:
            with self._replenish_lock:
                self._replenishing.discard(old_key)

    def _do_replenish_sync(self, old_key: str, acct_idx: int):
        """Synchronous fallback (no running event loop)."""
        try:
            new_key = self._generate_and_swap_pw(old_key, acct_idx)
            if new_key:
                self._log_cb(f"✅ Replenished: {old_key[:12]}… → {new_key[:12]}…")
            else:
                self._log_cb("✗ Replenish returned no key (sync fallback)")
        except Exception as e:
            self._log_cb(f"❌ Replenish failed for {old_key[:12]}…: {e}")
        finally:
            with self._replenish_lock:
                self._replenishing.discard(old_key)

    # ── CDP replenish (primary) ──────────────────────────────────────

    async def _generate_and_swap_cdp(
        self, acct_name: str, old_key: str, acct_idx: int
    ) -> str | None:
        """Generate key via CDP for *acct_name* and swap it in."""
        import websockets

        from .cdp_keygen import read_ws_url

        ws_url = read_ws_url()
        if not ws_url:
            self._log_cb("  Chrome CDP not available")
            return None

        try:
            async with websockets.connect(ws_url, max_size=2**24, open_timeout=10) as ws:
                # ── Find the right target ──────────────────────────────
                await ws.send(json.dumps({"id": 1, "method": "Target.getTargets"}))
                resp = await asyncio.wait_for(ws.recv(), timeout=5)
                resp_data = json.loads(resp) if isinstance(resp, (str, bytes)) else resp
                targets = resp_data.get("result", {}).get("targetInfos", [])

                api_pages = [
                    t
                    for t in targets
                    if t.get("type") == "page" and "settings/api-keys" in t.get("url", "")
                ]

            # ── Scan each context for the matching account ─────────────
            for t in api_pages:
                ctx = t.get("browserContextId", "")[:12]
                try:
                    new_key = await self._cdp_generate_for_target(
                        ws_url,
                        t["targetId"],
                        t.get("browserContextId", ""),
                        acct_name,
                        old_key,
                        acct_idx,
                    )
                    if new_key:
                        return new_key
                except Exception as e:
                    self._log_cb(f"  ctx {ctx}: {e}")
                    continue

            self._log_cb(f"  No authenticated tab found for {acct_name}")
            return None

        except Exception as e:
            self._log_cb(f"  CDP error: {e}")
            return None

    async def _cdp_generate_for_target(
        self,
        ws_url: str,
        target_id: str,
        ctx_id: str,
        acct_name: str,
        old_key: str,
        acct_idx: int,
    ) -> str | None:
        """Attach to a single CDP target, verify account match, generate key."""
        import websockets

        async with websockets.connect(ws_url, max_size=2**24, open_timeout=10) as ws:
            from .cdp_keygen import attach_and_get_sid, generate_one_key

            sid = await attach_and_get_sid(ws, target_id)
            if not sid:
                return None

            # Check auth and account name
            from .cdp_keygen import _eval_val, is_authenticated

            if not await is_authenticated(ws, sid):
                return None

            page_name = (
                await _eval_val(
                    ws,
                    sid,
                    """
                (() => {
                    const m = document.body.innerText.match(/within ([^\n]+)/);
                    return m ? m[1].trim() : '';
                })();
            """,
                    msg_id=401,
                )
            ) or ""

            if acct_name.lower() not in page_name.lower():
                return None

            # Generate key
            new_key = await generate_one_key(ws, sid, ws_url)

            if new_key and new_key.startswith("nvapi-"):
                self._swap_key(old_key, new_key, acct_idx)
                return new_key

            return None

    # ── Playwright replenish (fallback) ─────────────────────────────

    def _generate_and_swap_pw(self, old_key: str, acct_idx: int) -> str | None:
        """Generate via Playwright — tries Chrome channel first, then fallbacks."""
        acct = self.accounts[acct_idx]

        # 1. Chrome channel with isolated profile per account
        try:
            from .key_factory import chrome_channel_generate_key

            profile_dir = str(Path.home() / ".config" / "openvidia" / "profiles" / acct.name)
            self._log_cb(f"  Opening Chrome profile for {acct.name}…")
            new_key = chrome_channel_generate_key(
                old_key=old_key,
                user_data_dir=profile_dir,
            )
            if new_key:
                self._swap_key(old_key, new_key, acct_idx)
                return new_key
        except Exception as e:
            self._log_cb(f"  Chrome profile failed: {e}")

        # 2. email+password login
        if acct.has_credentials:
            from .key_factory import login_generate_key as gf

            new_key = gf(acct.email, acct.password, old_key=old_key)
            if new_key:
                self._swap_key(old_key, new_key, acct_idx)
                return new_key

        # 3. cookie injection (legacy, cookies expire quickly)
        if acct.cookie_json:
            from .key_factory import generate_key as gf

            new_key = gf(acct.cookie_json, old_key=old_key)
            if new_key:
                self._swap_key(old_key, new_key, acct_idx)
                return new_key

        self._log_cb(f"⚠ All keygen methods failed for {acct.name!r}")
        return None

    # ── Key swap helper ─────────────────────────────────────────────

    def _swap_key(self, old_key: str, new_key: str, acct_idx: int):
        """Swap *old_key* for *new_key* in both account and proxy state."""
        acct = self.accounts[acct_idx]

        try:
            i = acct.keys.index(old_key)
            acct.keys[i] = new_key
        except ValueError:
            acct.keys.append(new_key)

        with self._state_keys_lock:
            try:
                i = self.state.keys.index(old_key)
                self.state.keys[i] = new_key
            except ValueError:
                self.state.keys.append(new_key)

        self._key_owner[new_key] = acct_idx
        self._key_owner.pop(old_key, None)
        self.save()
        config.save_keys_file(list(self.state.keys))

    # ── health check ──────────────────────────────────────────────────

    async def health_check_loop(self, interval: float = 300.0):
        """Periodically verify all keys; drop dead ones."""
        while True:
            await asyncio.sleep(interval)
            self._log_cb("🔍 Running key health check…")
            dead = await asyncio.to_thread(self._probe_keys)
            for key in dead:
                self.on_key_failed(key)

    def _probe_keys(self) -> list[str]:
        """Quick HTTP probe of all keys. Returns list of dead keys."""
        import httpx

        dead = []
        for acct in self.accounts:
            for key in list(acct.keys):
                try:
                    r = httpx.get(
                        "https://integrate.api.nvidia.com/v1/models",
                        headers={"Authorization": f"Bearer {key}"},
                        timeout=8.0,
                    )
                    if r.status_code in (401, 403):
                        dead.append(key)
                except Exception:
                    dead.append(key)
                time.sleep(0.1)  # gentle pace
        return dead

    # ── helpers ───────────────────────────────────────────────────────

    def _find(self, name: str) -> Account | None:
        for a in self.accounts:
            if a.name == name:
                return a
        return None
