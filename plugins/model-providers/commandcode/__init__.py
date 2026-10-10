"""CommandCode provider profiles: ``commandcode`` (chat_completions) and
``commandcode-anthropic`` (anthropic_messages, Bearer auth — see
``agent/anthropic_adapter.py``). Same key and base URL for both."""

import json
import logging
import urllib.request

from hermes_cli.urllib_security import open_credentialed_url
from providers import get_provider_profile, register_provider
from providers.base import ProviderProfile, _profile_user_agent

logger = logging.getLogger(__name__)

_COMMANDCODE_BASE = "https://api.commandcode.ai/provider/v1"
_COMMANDCODE_MODELS_URL = f"{_COMMANDCODE_BASE}/models"


def _commandcode_models_url(base_url: str | None) -> str:
    """Resolve the /models URL. The picker passes base_url unconditionally, so
    only a value differing from the default is a custom endpoint."""
    caller_base = (base_url or "").strip().rstrip("/")
    custom = caller_base and caller_base != _COMMANDCODE_BASE
    return caller_base + "/models" if custom else _COMMANDCODE_MODELS_URL


def _fetch_commandcode_model_records(
    timeout: float = 10.0,
    base_url: str | None = None,
) -> list[dict] | None:
    """Fetch the live model catalog from the CommandCode /models endpoint.

    Returns the raw records (each with ``id`` and, usually,
    ``supported_endpoints``), or None on failure.
    No auth required — the public models endpoint is open.
    """
    try:
        req = urllib.request.Request(_commandcode_models_url(base_url))
        req.add_header("Accept", "application/json")
        req.add_header("User-Agent", _profile_user_agent())
        with open_credentialed_url(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
        # Response shape: {"object": "list", "data": [{"id": "...", ...}]}
        return [
            m
            for m in data.get("data", [])
            if isinstance(m, dict) and "id" in m
        ]
    except Exception as exc:
        logger.debug("fetch_models(commandcode): %s", exc)
        return None


# ── Wire-protocol filtering ───────────────────────────────────────────────────
# CommandCode fronts many vendors from one base URL, and each catalog entry
# publishes the endpoint(s) it is served on. A Claude model on this provider is
# served ONLY by /messages (Anthropic Messages shape); every other family is
# served by /chat/completions or /responses. Choosing the wrong wire is a hard
# 400, not a fallback:
#
#   400 Model "claude-sonnet-5-5" must be called via /provider/v1/messages
#       (Anthropic Messages shape).   [code: unsupported_model]
#
# Both profiles read the same public catalog, so each must advertise only the
# models it can actually serve — otherwise the picker offers a model under a
# provider that can never answer it, and selecting it fails on the first turn.

_CHAT_COMPLETIONS_ENDPOINTS = ("/chat/completions", "/responses")
_MESSAGES_ENDPOINTS = ("/messages",)


def _models_supporting_endpoint(
    records: list[dict], endpoints: tuple[str, ...]
) -> list[str]:
    """Model ids from *records* served on at least one of *endpoints*.

    A record advertising no ``supported_endpoints`` is kept: the field is
    advisory, and dropping every entry when it is absent would empty the picker
    behind a proxy or an older catalog revision. The gate only removes models
    that positively declare themselves served on another wire.
    """
    wanted = {endpoint.lower() for endpoint in endpoints}
    ids: list[str] = []
    for record in records:
        advertised = record.get("supported_endpoints")
        if isinstance(advertised, list) and advertised:
            served = {
                str(entry).strip().lower()
                for entry in advertised
                if str(entry).strip()
            }
            if not served.intersection(wanted):
                continue
        ids.append(str(record["id"]))
    return ids


class CommandCodeProfile(ProviderProfile):
    """CommandCode — OpenAI-compatible chat completions endpoint."""

    def fetch_models(
        self, *, api_key: str | None = None, base_url: str | None = None, timeout: float = 8.0
    ) -> list[str] | None:
        """Fetch the public CommandCode /models endpoint.

        Returns only models served on the OpenAI wire; /messages-only models
        (every claude-* entry) are dropped — see _CHAT_COMPLETIONS_ENDPOINTS.
        """
        records = _fetch_commandcode_model_records(
            timeout=timeout, base_url=base_url
        )
        if records is None:
            return None
        return _models_supporting_endpoint(records, _CHAT_COMPLETIONS_ENDPOINTS)

    def build_api_kwargs_extras(
        self, *, reasoning_config: dict | None = None, model: str | None = None, **context
    ) -> tuple[dict, dict]:
        """DeepSeek ids (``deepseek/deepseek-v4-flash``) get the native DeepSeek wire
        controls: DeepSeek V4+ defaults to thinking when ``thinking`` is omitted, so
        without them ``/reasoning`` never reaches the request (#95232). Other model
        families stay a no-op — CommandCode declares no reasoning vocabulary for them."""
        m = (model or "").strip()
        if not m.lower().startswith("deepseek/"):
            return {}, {}
        # Registry lookup, not a module import: the deepseek shim is only a loader-injected
        # sys.modules entry, and the registry honours a user override of the profile.
        native = get_provider_profile("deepseek")
        if native is None:
            return {}, {}
        return native.build_api_kwargs_extras(
            reasoning_config=reasoning_config, model=m.split("/", 1)[1], **context,
        )


class CommandCodeAnthropicProfile(CommandCodeProfile):
    """CommandCode — Anthropic Messages API-compatible endpoint."""

    def fetch_models(
        self, *, api_key: str | None = None, base_url: str | None = None, timeout: float = 8.0
    ) -> list[str] | None:
        """Fetch the public CommandCode /models endpoint.

        Filter to Anthropic-family models (claude-*) that are served on the
        Messages wire. The endpoint check is the mirror of the chat profile's:
        a claude-* entry advertising only /chat/completions would fail the same
        way in reverse.
        """
        records = _fetch_commandcode_model_records(
            timeout=timeout, base_url=base_url
        )
        if records is None:
            return None
        return [
            model
            for model in _models_supporting_endpoint(records, _MESSAGES_ENDPOINTS)
            if model.startswith("claude-")
        ]


commandcode = CommandCodeProfile(
    name="commandcode", aliases=("commandcode-chat",), api_mode="chat_completions",
    # Same key as the anthropic profile; distinct base-URL override vars so each
    # profile renders its own card on the desktop Keys tab (rows keyed by env var).
    env_vars=("COMMANDCODE_API_KEY", "COMMANDCODE_BASE_URL"),
    display_name="CommandCode", description="CommandCode — 20+ models via OpenAI-compatible API",
    signup_url="https://commandcode.ai/", base_url=_COMMANDCODE_BASE, models_url=_COMMANDCODE_MODELS_URL,
    fallback_models=(
        "deepseek/deepseek-v4-pro", "deepseek/deepseek-v4-flash", "Qwen/Qwen3.7-Max", "Qwen/Qwen3.6-Plus",
        "moonshotai/Kimi-K2.6", "zai-org/GLM-5.1", "MiniMaxAI/MiniMax-M2.7", "stepfun/Step-3.5-Flash",
        "xiaomi/mimo-v2.5-pro", "google/gemini-3.5-flash", "gpt-5.5",
    ),
    default_aux_model="deepseek/deepseek-v4-flash",
)

commandcode_anthropic = CommandCodeAnthropicProfile(
    name="commandcode-anthropic", aliases=("commandcode-claude",), api_mode="anthropic_messages",
    env_vars=("COMMANDCODE_API_KEY", "COMMANDCODE_ANTHROPIC_BASE_URL"),
    display_name="CommandCode (Anthropic)",
    description="CommandCode — Claude models via Anthropic Messages API",
    signup_url="https://commandcode.ai/", base_url=_COMMANDCODE_BASE, models_url=_COMMANDCODE_MODELS_URL,
    fallback_models=("claude-sonnet-4-6", "claude-opus-4-7", "claude-haiku-4-5-20251001"),
    default_aux_model="claude-haiku-4-5-20251001",
)

register_provider(commandcode)
register_provider(commandcode_anthropic)
