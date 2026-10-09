"""``hermes usage --all`` — every configured provider's real quota/balance as one JSON document.

Contract consumed by the desktop usage panel (keys are EXACT — extend only by adding keys)::

    {
      "schema_version": 1,
      "updated_at": "<ISO-8601>",
      "providers": [{
        "id": str, "label": str,
        "state": "available" | "unavailable" | "error",
        "kind": "subscription" | "balance" | "local",
        "detail": str,
        "windows": [{"label": str, "remaining_percent": 0..100, "reset_at": ISO-8601 | null}],
        "balance": {"amount": str, "currency": str},   # optional
        "plan": str,                                    # optional
      }]
    }

Invariants:

* **Read-only.** No credential writes, no token refresh, no pool rotation: Codex uses the native
  ``resolve_codex_runtime_credentials(read_only=True)`` path; Copilot uses the stored OAuth token
  as-is; DeepSeek reads the configured API key. Missing credentials are reported, never repaired.
* **Real data only.** Percentages come from the providers' own quota endpoints, finite, clamped to
  0..100, and converted used→remaining exactly once. Missing/invalid quotas are ``unavailable`` —
  never zero, never inferred from local inference token counts.
* **Sanitized failures.** Exception text is never stringified into the document; every failure
  detail is one of this module's static strings, so auth headers/tokens cannot leak.
* **Bounded.** Collectors run concurrently (<= ``MAX_WORKERS``) under one wall-clock deadline;
  per-call HTTP timeouts live in the collectors. No LLM calls.
"""

from __future__ import annotations

import math
import time
from datetime import datetime, timezone
from typing import Any, Optional, Sequence

SUBSCRIPTION_USAGE_SCHEMA_VERSION = 1

#: Concurrent collector ceiling and the whole-document wall-clock deadline.
MAX_WORKERS = 4
DEFAULT_DEADLINE_SECONDS = 20.0

#: Sanitized, stable failure details (never built from exception text).
_NO_CREDENTIALS_DETAIL = "No credentials are configured for this provider."
_FETCH_FAILED_DETAIL = "The provider usage endpoint could not be reached."
_NO_USAGE_DATA_DETAIL = "The provider returned no usage data."
_TIMED_OUT_DETAIL = "The usage query timed out."
_NO_ENDPOINT_DETAIL = "No quota endpoint is implemented for this provider."
_LOCAL_DETAIL = "Local provider — no subscription quota."

#: Loopback hostnames that mark a provider endpoint as local (no hosted quota).
_LOCAL_HOSTNAMES = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0"})
_LOCAL_PROVIDER_IDS = frozenset({"lmstudio", "lm-studio", "lm_studio", "llamacpp", "llama-cpp", "llama.cpp", "vllm"})


class SubscriptionUsageUnavailable(Exception):
    """A collector could not obtain usage because nothing is configured/returned.

    Distinct from transport errors: this maps to ``state="unavailable"`` while any other
    exception maps to ``state="error"``."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _finite_number(value: Any) -> Optional[float]:
    """Real number (int/float, not bool, finite) or None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _clamp_percent(value: float) -> float:
    return max(0.0, min(100.0, float(value)))


def _remaining_from_used(used: Any) -> Optional[float]:
    """Provider-reported percent-used → percent-remaining, clamped to 0..100."""
    number = _finite_number(used)
    return None if number is None else _clamp_percent(100.0 - number)


def _iso_or_none(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value is not None else None


def _parse_dt(value: Any) -> Optional[datetime]:
    """Native timestamp parser (epoch seconds or ISO-8601 string) — shared semantics."""
    from agent.account_usage import _parse_dt as native_parse_dt

    return native_parse_dt(value)


def _provider_label(provider_id: str) -> str:
    """Human label via native helpers; falls back to a title-cased id."""
    try:
        from hermes_cli.providers import get_label

        label = str(get_label(provider_id) or "").strip()
        if label and label != provider_id:
            return label
    except Exception:
        pass
    try:
        from hermes_cli.auth import get_auth_provider_display_name

        name = str(get_auth_provider_display_name(provider_id) or "").strip()
        if name and name != provider_id:
            return name
    except Exception:
        pass
    cleaned = provider_id.replace("_", " ").replace("-", " ").strip()
    return cleaned.title() if cleaned else provider_id


def _is_local_provider(provider_id: str) -> bool:
    """True for local endpoints (loopback base URL or a known local runtime id)."""
    normalized = (provider_id or "").strip().lower()
    if normalized in _LOCAL_PROVIDER_IDS:
        return True
    base_url = ""
    try:
        from providers import get_provider_profile

        profile = get_provider_profile(normalized)
        base_url = str(getattr(profile, "base_url", "") or "") if profile is not None else ""
    except Exception:
        base_url = ""
    if not base_url:
        try:
            from hermes_cli.providers import get_provider

            definition = get_provider(normalized, allow_network=False)
            base_url = str(getattr(definition, "base_url", "") or "") if definition is not None else ""
        except Exception:
            base_url = ""
    if not base_url:
        return False
    try:
        from urllib.parse import urlparse

        host = (urlparse(base_url).hostname or "").strip().lower().rstrip(".")
    except Exception:
        return False
    return host in _LOCAL_HOSTNAMES


def _entry(
    provider_id: str,
    *,
    state: str,
    kind: str,
    detail: str,
    windows: Optional[list[dict]] = None,
    balance: Optional[dict] = None,
    plan: Optional[str] = None,
) -> dict:
    entry: dict[str, Any] = {
        "id": provider_id,
        "label": _provider_label(provider_id),
        "state": state,
        "kind": kind,
        "detail": detail,
        "windows": list(windows or ()),
    }
    if balance is not None:
        entry["balance"] = balance
    if plan:
        entry["plan"] = plan
    return entry


def _http_get_json(url: str, headers: dict, *, timeout: float) -> dict:
    """One bounded GET returning a JSON object (raises on transport/HTTP/parse failure)."""
    import httpx

    with httpx.Client(timeout=timeout) as client:
        response = client.get(url, headers=headers)
        response.raise_for_status()
    payload = response.json()
    return payload if isinstance(payload, dict) else {}


# ── openai-codex (ChatGPT subscription windows; native read-only credential path) ─────────────

_CODEX_TIMEOUT_SECONDS = 15.0


def _codex_usage_payload() -> dict:
    """GET the Codex usage endpoint with the stored credential — read-only, no refresh.

    ``resolve_codex_runtime_credentials(read_only=True)`` reports the stored state as-is: no Codex
    CLI adoption, no token refresh, no auth-store write. A stale token surfaces as a fetch error
    entry; it is never refreshed from a quota query.
    """
    from agent.account_usage import _codex_backend_urls, _codex_headers
    from hermes_cli.auth_codex import resolve_codex_runtime_credentials

    try:
        credentials = resolve_codex_runtime_credentials(read_only=True)
    except Exception as exc:
        raise SubscriptionUsageUnavailable() from exc
    token = str(credentials.get("api_key") or "").strip()
    if not token:
        raise SubscriptionUsageUnavailable()
    base_url = str(credentials.get("base_url") or "").strip()
    account_id: Optional[str] = None
    try:
        from hermes_cli.auth import _read_codex_tokens

        tokens = _read_codex_tokens(_lock=False).get("tokens") or {}
        account_id = str(tokens.get("account_id", "") or "").strip() or None
    except Exception:
        account_id = None  # best-effort; the usage endpoint works without it on most accounts
    usage_url = _codex_backend_urls(base_url)[0]
    return _http_get_json(usage_url, _codex_headers(token, account_id), timeout=_CODEX_TIMEOUT_SECONDS)


def _codex_windows(payload: dict) -> list[dict]:
    from agent.account_usage import _codex_window_labels

    rate_limit = payload.get("rate_limit")
    if not isinstance(rate_limit, dict):
        return []
    windows: list[dict] = []
    for key, label in _codex_window_labels(rate_limit):
        window = rate_limit.get(key)
        if not isinstance(window, dict):
            continue
        remaining = _remaining_from_used(window.get("used_percent"))
        if remaining is None:
            continue
        windows.append(
            {"label": label, "remaining_percent": remaining, "reset_at": _iso_or_none(_parse_dt(window.get("reset_at")))}
        )
    return windows


def _codex_balance(payload: dict) -> Optional[dict]:
    credits = payload.get("credits")
    if not isinstance(credits, dict) or not credits.get("has_credits"):
        return None
    balance = credits.get("balance")
    number = _finite_number(balance)
    if number is None:
        try:
            number = _finite_number(float(str(balance).strip()))
        except (TypeError, ValueError):
            return None
    if number is None:
        return None
    return {"amount": f"{number:.2f}", "currency": "USD"}


def _codex_detail(payload: dict, balance: Optional[dict]) -> str:
    parts = ["ChatGPT subscription usage"]
    resets = payload.get("rate_limit_reset_credits")
    count = _finite_number((resets or {}).get("available_count")) if isinstance(resets, dict) else None
    if count and count > 0:
        plural = "" if int(count) == 1 else "s"
        parts.append(f"{int(count)} banked rate-limit reset{plural}")
    if balance is None and isinstance(payload.get("credits"), dict) and payload["credits"].get("unlimited"):
        parts.append("credits unlimited")
    return " — ".join(parts) + "."


def _collect_codex() -> dict:
    try:
        payload = _codex_usage_payload()
    except SubscriptionUsageUnavailable:
        return _entry("openai-codex", state="unavailable", kind="subscription", detail=_NO_CREDENTIALS_DETAIL)
    except Exception:
        return _entry("openai-codex", state="error", kind="subscription", detail=_FETCH_FAILED_DETAIL)
    if not isinstance(payload, dict):
        return _entry("openai-codex", state="unavailable", kind="subscription", detail=_NO_USAGE_DATA_DETAIL)
    windows = _codex_windows(payload)
    balance = _codex_balance(payload)
    if not windows and balance is None:
        return _entry("openai-codex", state="unavailable", kind="subscription", detail=_NO_USAGE_DATA_DETAIL)
    try:
        from agent.account_usage import _title_case_slug

        plan = _title_case_slug(payload.get("plan_type"))
    except Exception:
        plan = None
    return _entry(
        "openai-codex",
        state="available",
        kind="subscription",
        detail=_codex_detail(payload, balance),
        windows=windows,
        balance=balance,
        plan=plan,
    )


# ── collector dispatch ────────────────────────────────────────────────────────────────────────


def _provider_entry(provider_id: str) -> dict:
    if provider_id == "openai-codex":
        return _collect_codex()
    if provider_id == "deepseek":
        return _collect_deepseek()
    if provider_id == "copilot":
        return _collect_copilot()
    if _is_local_provider(provider_id):
        return _entry(provider_id, state="unavailable", kind="local", detail=_LOCAL_DETAIL)
    return _entry(provider_id, state="unavailable", kind="subscription", detail=_NO_ENDPOINT_DETAIL)


def _safe_provider_entry(provider_id: str) -> dict:
    """Provider entry that never raises: any unknown failure is a sanitized error entry."""
    try:
        return _provider_entry(provider_id)
    except Exception:
        return _entry(provider_id, state="error", kind="subscription", detail=_FETCH_FAILED_DETAIL)


def _collect_all(provider_ids: Sequence[str], deadline_seconds: float) -> list[dict]:
    """Run every collector concurrently under one wall-clock deadline, preserving order."""
    if not provider_ids:
        return []
    from concurrent.futures import TimeoutError as FuturesTimeout
    from tools.daemon_pool import DaemonThreadPoolExecutor

    deadline = time.monotonic() + max(0.05, float(deadline_seconds))
    pool = DaemonThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(provider_ids)))
    try:
        futures = {provider_id: pool.submit(_safe_provider_entry, provider_id) for provider_id in provider_ids}
        entries: list[dict] = []
        for provider_id in provider_ids:
            remaining = max(0.0, deadline - time.monotonic())
            try:
                # timeout=0 still returns an already-completed future: a slow sibling consuming the
                # budget must not mask results that landed while it ran.
                entries.append(futures[provider_id].result(timeout=remaining))
            except FuturesTimeout:
                entries.append(_entry(provider_id, state="error", kind="subscription", detail=_TIMED_OUT_DETAIL))
            except Exception:
                entries.append(_entry(provider_id, state="error", kind="subscription", detail=_FETCH_FAILED_DETAIL))
        return entries
    finally:
        # Never join abandoned workers: a provider endpoint that accepts the connection but never
        # answers must not hold the CLI (mirrors agent.account_usage's bounded fetch).
        pool.shutdown(wait=False)


def build_subscription_usage_document(
    *, provider_ids: Optional[Sequence[str]] = None, deadline_seconds: float = DEFAULT_DEADLINE_SECONDS
) -> dict:
    """Build the ``hermes usage --all`` JSON document for every configured provider."""
    raw_ids = list(provider_ids) if provider_ids is not None else _configured_provider_ids()
    ordered: list[str] = []
    seen: set[str] = set()
    for candidate in raw_ids:
        provider_id = str(candidate or "").strip()
        if provider_id and provider_id not in seen:
            seen.add(provider_id)
            ordered.append(provider_id)
    return {
        "schema_version": SUBSCRIPTION_USAGE_SCHEMA_VERSION,
        "updated_at": _utc_now().isoformat(),
        "providers": _collect_all(ordered, deadline_seconds),
    }


# ── deepseek (prepaid balance; documented GET /user/balance) ────────────────────────────────────

_DEEPSEEK_TIMEOUT_SECONDS = 15.0
_DEEPSEEK_BALANCE_URL = "https://api.deepseek.com/user/balance"
_DEEPSEEK_ENV_VARS = ("DEEPSEEK_API_KEY",)


def _read_only_api_key(env_vars: Sequence[str]) -> str:
    """First usable secret among *env_vars* via the native scoped reader — never pool machinery."""
    from hermes_cli.auth import has_usable_secret
    from hermes_cli.config import get_env_value_prefer_dotenv

    for name in env_vars:
        value = str(get_env_value_prefer_dotenv(name) or "").strip()
        if value and has_usable_secret(value):
            return value
    return ""


def _deepseek_env_vars() -> Sequence[str]:
    try:
        from providers import get_provider_profile

        profile = get_provider_profile("deepseek")
        env_vars = tuple(getattr(profile, "env_vars", ()) or ())
        if env_vars:
            return env_vars
    except Exception:
        pass
    return _DEEPSEEK_ENV_VARS


def _deepseek_balance_payload() -> dict:
    """GET the documented DeepSeek balance endpoint with the configured API key (read-only)."""
    api_key = _read_only_api_key(_deepseek_env_vars())
    if not api_key:
        raise SubscriptionUsageUnavailable()
    headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
    return _http_get_json(_DEEPSEEK_BALANCE_URL, headers, timeout=_DEEPSEEK_TIMEOUT_SECONDS)


def _deepseek_balance(payload: dict) -> Optional[tuple[dict, str]]:
    """(balance row, detail) from the documented ``balance_infos`` array, or None when no row is
    a valid currency/amount pair — a missing balance must read as unavailable, never as zero."""
    infos = payload.get("balance_infos")
    if not isinstance(infos, list):
        return None
    rows: list[tuple[str, str]] = []
    for info in infos:
        if not isinstance(info, dict):
            continue
        currency = str(info.get("currency") or "").strip()
        amount = info.get("total_balance")
        if not currency or not isinstance(amount, str) or not amount.strip():
            continue
        try:
            if _finite_number(float(amount.strip())) is None:
                continue  # reject NaN, infinity and overflow before rendering or serialization
        except ValueError:
            continue
        rows.append((currency, amount.strip()))
    if not rows:
        return None
    detail = "Prepaid API balance: " + ", ".join(f"{currency} {amount}" for currency, amount in rows) + "."
    if payload.get("is_available") is False:
        detail += " Balance is insufficient for API calls."
    return {"amount": rows[0][1], "currency": rows[0][0]}, detail


def _collect_deepseek() -> dict:
    try:
        payload = _deepseek_balance_payload()
    except SubscriptionUsageUnavailable:
        return _entry("deepseek", state="unavailable", kind="balance", detail=_NO_CREDENTIALS_DETAIL)
    except Exception:
        return _entry("deepseek", state="error", kind="balance", detail=_FETCH_FAILED_DETAIL)
    parsed = _deepseek_balance(payload) if isinstance(payload, dict) else None
    if parsed is None:
        return _entry("deepseek", state="unavailable", kind="balance", detail=_NO_USAGE_DATA_DETAIL)
    balance, detail = parsed
    return _entry("deepseek", state="available", kind="balance", detail=detail, balance=balance)


# ── copilot (native quota endpoint used by GitHub's own clients) ────────────────────────────────

_COPILOT_TIMEOUT_SECONDS = 15.0
_COPILOT_QUOTA_URL = "https://api.github.com/copilot_internal/user"
_COPILOT_WINDOW_LABELS = {"chat": "Chat", "completions": "Completions", "premium_interactions": "Premium requests"}


def _copilot_quota_payload() -> dict:
    """GET the Copilot quota endpoint with the stored GitHub OAuth token as-is (read-only)."""
    try:
        from hermes_cli.copilot_auth import resolve_copilot_token

        token, _source = resolve_copilot_token()
    except Exception as exc:
        raise SubscriptionUsageUnavailable() from exc
    token = str(token or "").strip()
    if not token:
        raise SubscriptionUsageUnavailable()
    try:
        from hermes_cli.models import copilot_default_headers

        headers = dict(copilot_default_headers(is_agent_turn=False))
    except Exception:
        headers = {}
    headers.update({"Authorization": f"token {token}", "Accept": "application/json"})
    return _http_get_json(_COPILOT_QUOTA_URL, headers, timeout=_COPILOT_TIMEOUT_SECONDS)


def _copilot_window_label(key: str) -> str:
    label = _COPILOT_WINDOW_LABELS.get(key)
    if label:
        return label
    cleaned = str(key or "").replace("_", " ").strip()
    return cleaned.title() if cleaned else "Quota"


def _copilot_windows(payload: dict) -> list[dict]:
    snapshots = payload.get("quota_snapshots")
    if not isinstance(snapshots, dict):
        return []
    fallback_reset_raw = payload.get("quota_reset_date_utc") or payload.get("quota_reset_date")
    fallback_reset = _iso_or_none(_parse_dt(fallback_reset_raw))
    windows: list[dict] = []
    for key, snapshot in snapshots.items():
        if not isinstance(snapshot, dict):
            continue
        remaining = _finite_number(snapshot.get("percent_remaining"))
        if remaining is None:
            # Fallback: entitlement (total) and quota_remaining (left) are both present.
            entitlement = _finite_number(snapshot.get("entitlement"))
            quota_remaining = _finite_number(snapshot.get("quota_remaining"))
            if entitlement and entitlement > 0 and quota_remaining is not None:
                remaining = _clamp_percent(quota_remaining / entitlement * 100.0)
        if remaining is None:
            continue
        reset_at = None
        snapshot_reset = _finite_number(snapshot.get("quota_reset_at"))
        if snapshot_reset and snapshot_reset > 0:
            reset_at = _iso_or_none(_parse_dt(snapshot_reset))
        windows.append(
            {
                "label": _copilot_window_label(key),
                "remaining_percent": _clamp_percent(remaining),
                "reset_at": reset_at or fallback_reset,
            }
        )
    return windows


def _collect_copilot() -> dict:
    try:
        payload = _copilot_quota_payload()
    except SubscriptionUsageUnavailable:
        return _entry("copilot", state="unavailable", kind="subscription", detail=_NO_CREDENTIALS_DETAIL)
    except Exception:
        return _entry("copilot", state="error", kind="subscription", detail=_FETCH_FAILED_DETAIL)
    windows = _copilot_windows(payload) if isinstance(payload, dict) else []
    if not windows:
        return _entry("copilot", state="unavailable", kind="subscription", detail=_NO_USAGE_DATA_DETAIL)
    plan = None
    try:
        from agent.account_usage import _title_case_slug

        plan = _title_case_slug(payload.get("copilot_plan"))
    except Exception:
        plan = None
    return _entry(
        "copilot",
        state="available",
        kind="subscription",
        detail="GitHub Copilot account usage.",
        windows=windows,
        plan=plan,
    )


# ── configured-provider discovery (native auth/pool/config/env APIs only) ──────────────────────

#: Env vars set by third-party CLIs, not by the user configuring Hermes — never a config signal.
_IMPLICIT_ENV_VARS_FALLBACK = frozenset({"CLAUDE_CODE_OAUTH_TOKEN"})


def _configured_provider_ids() -> list[str]:
    """Every provider this install actually has configured, via native readers:

    1. credential-pool keys (includes arbitrary/third-party ids),
    2. auth-store ``providers`` section keys (OAuth logins),
    3. the configured model provider,
    4. named providers under ``providers.<name>`` in config.yaml (custom endpoints),
    5. registered profiles whose declared env var currently resolves to a usable secret.

    Never guesses: unconfigured registry entries and pure settings buckets stay out.
    """
    ids: list[str] = []

    def add(value) -> None:
        cleaned = str(value or "").strip().lower()
        if cleaned:
            ids.append(cleaned)

    try:
        from hermes_cli.auth import read_credential_pool

        pool = read_credential_pool()
        if isinstance(pool, dict):
            for provider_id in pool:
                add(provider_id)
    except Exception:
        pass

    try:
        from hermes_cli.auth import _load_auth_store

        store = _load_auth_store()
        for provider_id in (store.get("providers") or {}) if isinstance(store, dict) else ():
            add(provider_id)
    except Exception:
        pass

    try:
        from hermes_cli.runtime_provider import resolve_requested_provider

        requested = str(resolve_requested_provider() or "").strip().lower()
        if requested and requested != "auto":
            add(requested)
    except Exception:
        pass

    try:
        from hermes_cli.config import load_config

        providers_cfg = load_config().get("providers")
        if isinstance(providers_cfg, dict):
            for name, value in providers_cfg.items():
                # A provider entry carries an endpoint or a key pointer; bare settings buckets
                # (the ``custom`` timeout/models block) are not providers.
                if isinstance(value, dict) and ("base_url" in value or "key_env" in value):
                    add(name)
    except Exception:
        pass

    try:
        import providers as providers_registry
        from hermes_cli.auth import has_usable_secret
        from hermes_cli.config import get_env_value_prefer_dotenv

        implicit = _IMPLICIT_ENV_VARS_FALLBACK
        try:
            from hermes_cli.auth import _IMPLICIT_ENV_VARS

            implicit = frozenset(_IMPLICIT_ENV_VARS) | implicit
        except Exception:
            pass
        for profile in providers_registry.list_providers():
            env_vars = tuple(getattr(profile, "env_vars", ()) or ())
            for env_name in env_vars:
                if env_name in implicit:
                    continue
                value = str(get_env_value_prefer_dotenv(env_name) or "").strip()
                if value and has_usable_secret(value):
                    add(getattr(profile, "name", ""))
                    break
    except Exception:
        pass

    ordered: list[str] = []
    seen: set[str] = set()
    for provider_id in ids:
        if provider_id not in seen:
            seen.add(provider_id)
            ordered.append(provider_id)
    return ordered


# ── human-readable rendering ──────────────────────────────────────────────────────────────────


def render_subscription_usage_lines(document: dict) -> list[str]:
    """Human-readable ``hermes usage --all`` lines (the JSON document stays canonical)."""
    lines = ["📈 Subscription usage"]
    for entry in document.get("providers") or []:
        if not isinstance(entry, dict):
            continue
        label = str(entry.get("label") or entry.get("id") or "").strip()
        state = str(entry.get("state") or "")
        parts = [f"{label}: {state}" if label else state]
        for window in entry.get("windows") or []:
            if not isinstance(window, dict):
                continue
            percent = _finite_number(window.get("remaining_percent"))
            window_label = str(window.get("label") or "Quota")
            parts.append(f"{window_label}: {round(percent)}% remaining" if percent is not None else f"{window_label}: unavailable")
        balance = entry.get("balance")
        if isinstance(balance, dict) and str(balance.get("amount") or "").strip():
            parts.append(f"Balance: {str(balance['amount']).strip()} {str(balance.get('currency') or '').strip()}".strip())
        if state != "available" and entry.get("detail"):
            parts.append(str(entry["detail"]))
        plan = str(entry.get("plan") or "").strip()
        lines.append(f"  • {' — '.join(parts)}" + (f" ({plan})" if plan and state == "available" else ""))
    return lines
