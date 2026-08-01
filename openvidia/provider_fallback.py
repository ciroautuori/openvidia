"""Optional free-tier provider fallback for /v1/chat/completions.

When every NVIDIA key is exhausted, the proxy can hand the request to an
alternative OpenAI-compatible endpoint (DeepSeek, GLM, ...) configured in
providers.json. Disabled by default: nothing is loaded unless the user opts
in with real API keys.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

import httpx

from . import config

_TIMEOUT_S = 30.0


@dataclass
class ProviderConfig:
    name: str
    base_url: str
    api_key: str
    models_map: dict[str, str] = field(default_factory=dict)


def load_provider_configs() -> list[ProviderConfig]:
    """Read providers.json; skip entries whose api_key_env is unresolved."""
    path = config.config_dir() / "providers.json"
    if not path.exists():
        return []
    raw = path.read_text(encoding="utf-8")
    try:
        entries = json.loads(raw)
    except (ValueError, TypeError):
        # Un JSON corrotto non deve mai far cadere il proxy: si ignora.
        return []
    if not isinstance(entries, list):
        return []
    providers: list[ProviderConfig] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        base_url = entry.get("base_url")
        api_key_env = entry.get("api_key_env")
        if not isinstance(name, str) or not isinstance(base_url, str):
            continue
        if not isinstance(api_key_env, str) or not os.environ.get(api_key_env):
            continue
        models_map = entry.get("models") or {}
        providers.append(
            ProviderConfig(
                name=name,
                base_url=base_url.rstrip("/"),
                api_key=os.environ[api_key_env],
                models_map=models_map if isinstance(models_map, dict) else {},
            )
        )
    return providers


def rewrite_payload(payload: dict, provider: ProviderConfig) -> dict:
    """Map the requested NVIDIA model onto the provider's own model id."""
    copied = dict(payload)
    model = copied.get("model", "")
    if isinstance(model, str) and model in provider.models_map:
        copied["model"] = provider.models_map[model]
    elif provider.models_map:
        copied["model"] = next(iter(provider.models_map.values()))
    return copied


async def try_fallback(
    client: httpx.AsyncClient,
    payload: dict,
    provider: ProviderConfig,
    stream: bool = False,
) -> httpx.Response | None:
    """Attempt one fallback provider; None on any upstream failure."""
    try:
        resp = await client.post(
            provider.base_url + "/chat/completions",
            json=rewrite_payload(payload, provider),
            headers={
                "Authorization": f"Bearer {provider.api_key}",
                "User-Agent": "openvidia/2.0",
            },
            timeout=_TIMEOUT_S,
        )
    except httpx.HTTPError:
        return None
    if not resp.is_success:
        return None
    return resp
