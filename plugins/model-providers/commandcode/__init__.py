"""CommandCode provider profile.

CommandCode provides a unified API that fronts 20+ models from DeepSeek, Qwen,
Kimi, GLM, MiniMax, StepFun, Xiaomi Mimo, Google Gemini, and OpenAI GPT — all
accessible through either OpenAI-compatible chat completions or Anthropic
Messages endpoints from a single base URL and API key.

Two provider profiles are registered:

``commandcode``
    ``api_mode=chat_completions`` — standard OpenAI-compatible endpoint.
    Model prefix: ``deepseek/deepseek-v4-pro``, ``Qwen/Qwen3.7-Max``, etc.

``commandcode-anthropic``
    ``api_mode=anthropic_messages`` — Anthropic Messages API-compatible.
    Model names: ``claude-sonnet-4-6``, ``claude-opus-4-7``,
    ``claude-haiku-4-5-20251001``.

Both use the same ``COMMANDCODE_API_KEY`` env var and
``https://api.commandcode.ai/provider/v1`` base URL.  The
``commandcode-anthropic`` profile relies on ``agent/anthropic_adapter.py``
recognizing the ``api.commandcode.ai`` hostname for Bearer auth (the
CommandCode /anthropic endpoint uses ``Authorization: Bearer``, not
Anthropic's native ``x-api-key`` header).
"""

from __future__ import annotations

import json
import logging
import urllib.request

from providers import register_provider
from providers.base import ProviderProfile, _profile_user_agent

logger = logging.getLogger(__name__)

# ── Shared constants ──────────────────────────────────────────────────────────
_COMMANDCODE_BASE = "https://api.commandcode.ai/provider/v1"
_COMMANDCODE_MODELS_URL = f"{_COMMANDCODE_BASE}/models"
# Both profiles authenticate with the same key; each carries its own base-URL
# override var so each renders its own card on the desktop Keys tab (rows are
# keyed by env var, and the shared API key attributes to the first profile).
_COMMANDCODE_ENV = ("COMMANDCODE_API_KEY", "COMMANDCODE_BASE_URL")
_COMMANDCODE_ANTHROPIC_ENV = ("COMMANDCODE_API_KEY", "COMMANDCODE_ANTHROPIC_BASE_URL")


def _fetch_commandcode_model_records(
    timeout: float = 10.0,
    base_url: str | None = None,
) -> list[dict] | None:
    """Fetch the live model catalog from the CommandCode /models endpoint.

    Returns the raw records (each with ``id`` and, usually,
    ``supported_endpoints``), or None on failure.
    No auth required — the public models endpoint is open.

    ``base_url`` overrides the endpoint only when the caller passed a URL
    that differs from the default ``_COMMANDCODE_BASE`` (a user-configured
    ``model.base_url`` / ``COMMANDCODE_BASE_URL`` pointing at a proxy or
    custom deployment). The picker passes base_url unconditionally, falling
    back to the profile default — equality means "not customised".
    """
    caller_base = (base_url or "").strip()
    if caller_base and caller_base.rstrip("/") != _COMMANDCODE_BASE.rstrip("/"):
        models_url = caller_base.rstrip("/") + "/models"
    else:
        models_url = _COMMANDCODE_MODELS_URL
    try:
        req = urllib.request.Request(models_url)
        req.add_header("Accept", "application/json")
        req.add_header("User-Agent", _profile_user_agent())
        with urllib.request.urlopen(req, timeout=timeout) as resp:
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


def _fetch_commandcode_models(
    timeout: float = 10.0,
    base_url: str | None = None,
) -> list[str] | None:
    """Flat model-id list across every wire — thin wrapper over the records."""
    records = _fetch_commandcode_model_records(timeout=timeout, base_url=base_url)
    if records is None:
        return None
    return [str(record["id"]) for record in records]


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


# ── Chat Completions profile ──────────────────────────────────────────────────

class CommandCodeProfile(ProviderProfile):
    """CommandCode — OpenAI-compatible chat completions endpoint."""

    def fetch_models(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = 8.0,
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


commandcode = CommandCodeProfile(
    name="commandcode",
    aliases=("commandcode-chat",),
    api_mode="chat_completions",
    env_vars=_COMMANDCODE_ENV,
    display_name="CommandCode",
    description="CommandCode — 20+ models via OpenAI-compatible API",
    signup_url="https://commandcode.ai/",
    base_url=_COMMANDCODE_BASE,
    models_url=_COMMANDCODE_MODELS_URL,
    fallback_models=(
        "deepseek/deepseek-v4-pro",
        "deepseek/deepseek-v4-flash",
        "Qwen/Qwen3.7-Max",
        "Qwen/Qwen3.6-Plus",
        "moonshotai/Kimi-K2.6",
        "zai-org/GLM-5.1",
        "MiniMaxAI/MiniMax-M2.7",
        "stepfun/Step-3.5-Flash",
        "xiaomi/mimo-v2.5-pro",
        "google/gemini-3.5-flash",
        "gpt-5.5",
    ),
    default_aux_model="deepseek/deepseek-v4-flash",
)


# ── Anthropic Messages profile ────────────────────────────────────────────────

class CommandCodeAnthropicProfile(ProviderProfile):
    """CommandCode — Anthropic Messages API-compatible endpoint.

    Uses Bearer auth (same API key), not Anthropic's native x-api-key header.
    ``agent/anthropic_adapter.py`` must recognize ``api.commandcode.ai``
    as a Bearer-auth domain for this to work.
    """

    def fetch_models(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = 8.0,
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


commandcode_anthropic = CommandCodeAnthropicProfile(
    name="commandcode-anthropic",
    aliases=("commandcode-claude",),
    api_mode="anthropic_messages",
    env_vars=_COMMANDCODE_ANTHROPIC_ENV,
    display_name="CommandCode (Anthropic)",
    description="CommandCode — Claude models via Anthropic Messages API",
    signup_url="https://commandcode.ai/",
    base_url=_COMMANDCODE_BASE,
    models_url=_COMMANDCODE_MODELS_URL,
    fallback_models=(
        "claude-sonnet-4-6",
        "claude-opus-4-7",
        "claude-haiku-4-5-20251001",
    ),
    default_aux_model="claude-haiku-4-5-20251001",
)


# ── Registration ──────────────────────────────────────────────────────────────
register_provider(commandcode)
register_provider(commandcode_anthropic)
