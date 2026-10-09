"""``hermes usage --all`` — multi-provider subscription/balance document (issue: desktop usage panel).

Unit tests drive the real ``hermes_cli.subscription_usage`` module; only the module's network /
credential seams are replaced, so parsing, percent semantics, sanitization, discovery and the
document schema under test are the production code paths.
"""

import json
import sys
import time
from datetime import datetime, timezone
from unittest.mock import patch

from agent.account_usage import AccountUsageSnapshot, AccountUsageWindow
from hermes_cli import main as hermes_main
from hermes_cli import subscription_usage
from hermes_cli.subscription_usage import build_subscription_usage_document


# ── real producer payload shapes (captured read-only from live endpoints 2026-10-05) ──────────

# GET https://chatgpt.com/backend-api/wham/usage (Codex, read-only resolve)
CODEX_PAYLOAD = {
    "plan_type": "prolite",
    "rate_limit": {
        "allowed": True,
        "limit_reached": False,
        "primary_window": {
            "used_percent": 56,
            "limit_window_seconds": 604800,
            "reset_at": 1791590446,
            "reset_after_seconds": 421642,
        },
        "secondary_window": {},
    },
    "credits": {"has_credits": False, "unlimited": False, "balance": "0"},
    "rate_limit_reset_credits": {"available_count": 3},
}


def _build(provider_ids, *, codex=None, deepseek=None, copilot=None, deadline_seconds=20.0):
    """Build the document with the network seams replaced by canned payloads."""
    patches = []
    for name, seam in (("_codex_usage_payload", codex), ("_deepseek_balance_payload", deepseek), ("_copilot_quota_payload", copilot)):
        if seam is None:
            continue
        if isinstance(seam, Exception):
            side_effect = seam
        elif callable(seam):
            side_effect = seam
        else:
            side_effect = lambda value=seam: value
        patches.append(patch.object(subscription_usage, name, side_effect=side_effect))
    with patch.object(subscription_usage, "_configured_provider_ids", return_value=list(provider_ids)):
        for p in patches:
            p.start()
        try:
            return build_subscription_usage_document(deadline_seconds=deadline_seconds)
        finally:
            for p in patches:
                p.stop()


def _provider(doc, pid):
    matches = [p for p in doc["providers"] if p["id"] == pid]
    assert matches, f"provider {pid} missing from {[p['id'] for p in doc['providers']]}"
    return matches[0]


# ── cycle 1: schema, Codex real shape, percent semantics ───────────────────────────────────────

def test_document_schema_is_exact_and_codex_weekly_window_converts_used_to_remaining():
    doc = _build(["openai-codex"], codex=CODEX_PAYLOAD)

    assert set(doc) == {"schema_version", "updated_at", "providers"}
    assert doc["schema_version"] == 1
    # updated_at is a parseable UTC ISO-8601 timestamp
    assert datetime.fromisoformat(doc["updated_at"]).tzinfo is not None

    entry = _provider(doc, "openai-codex")
    assert set(entry) == {"id", "label", "state", "kind", "detail", "windows", "plan"}
    assert entry["state"] == "available"
    assert entry["kind"] == "subscription"
    assert entry["plan"] == "Prolite"
    assert isinstance(entry["detail"], str)

    # 604800s primary window is the Weekly limit; 56% used → 44% remaining
    (window,) = entry["windows"]
    assert set(window) == {"label", "remaining_percent", "reset_at"}
    assert window["label"] == "Weekly"
    assert window["remaining_percent"] == 44.0
    assert window["reset_at"] == datetime.fromtimestamp(1791590446, tz=timezone.utc).isoformat()


def test_windows_only_emitted_for_finite_numeric_quota_values():
    payload = {
        "plan_type": "prolite",
        "rate_limit": {
            "primary_window": {"used_percent": 25, "limit_window_seconds": 604800},
            "secondary_window": {"used_percent": "not-a-number", "limit_window_seconds": 18000},
        },
        "credits": {},
    }
    entry = _provider(_build(["openai-codex"], codex=payload), "openai-codex")
    assert entry["state"] == "available"
    (window,) = entry["windows"]
    assert window["label"] == "Weekly" and window["remaining_percent"] == 75.0 and window["reset_at"] is None


def test_percentages_are_clamped_into_zero_to_one_hundred():
    payload = {
        "rate_limit": {
            "primary_window": {"used_percent": 105, "limit_window_seconds": 18000},
            "secondary_window": {"used_percent": -5, "limit_window_seconds": 604800},
        },
    }
    entry = _provider(_build(["openai-codex"], codex=payload), "openai-codex")
    session, weekly = entry["windows"]
    assert session["label"] == "Session" and session["remaining_percent"] == 0.0
    assert weekly["label"] == "Weekly" and weekly["remaining_percent"] == 100.0
    for window in entry["windows"]:
        assert 0.0 <= window["remaining_percent"] <= 100.0


def test_missing_quotas_are_unavailable_not_zero():
    payload = {"plan_type": "prolite", "rate_limit": {"primary_window": {}, "secondary_window": {}}}
    entry = _provider(_build(["openai-codex"], codex=payload), "openai-codex")
    assert entry["state"] == "unavailable"
    assert entry["windows"] == []
    assert "balance" not in entry


# ── cycle 2: DeepSeek / Copilot producers, sanitization, read-only credentials, local states ──

# GET https://api.deepseek.com/user/balance (captured read-only 2026-10-05)
DEEPSEEK_PAYLOAD = {
    "is_available": False,
    "balance_infos": [
        {"currency": "USD", "total_balance": "0.00", "granted_balance": "0.00", "topped_up_balance": "0.00"},
        {"currency": "CNY", "total_balance": "-0.24", "granted_balance": "0.00", "topped_up_balance": "-0.24"},
    ],
}

# GET https://api.github.com/copilot_internal/user (captured read-only 2026-10-05; premium
# percent mirrors the shape GitHub's own clients report)
COPILOT_PAYLOAD = {
    "copilot_plan": "individual",
    "quota_reset_date": "2026-11-01",
    "quota_snapshots": {
        "chat": {"percent_remaining": 100.0, "unlimited": True, "entitlement": 0, "quota_remaining": 0.0, "quota_reset_at": 0},
        "completions": {"percent_remaining": 100.0, "unlimited": True, "entitlement": 0, "quota_remaining": 0.0, "quota_reset_at": 0},
        "premium_interactions": {
            "percent_remaining": 36.3,
            "entitlement": 200,
            "quota_remaining": 72.6,
            "remaining": 72,
            "unlimited": False,
            "quota_reset_at": 0,
        },
    },
}


def test_deepseek_balance_real_shape_is_a_balance_provider_not_windows():
    entry = _provider(_build(["deepseek"], deepseek=DEEPSEEK_PAYLOAD), "deepseek")
    assert entry["state"] == "available"
    assert entry["kind"] == "balance"
    assert entry["windows"] == []
    # First balance row is the machine-readable balance; every row is named in the detail.
    assert entry["balance"] == {"amount": "0.00", "currency": "USD"}
    assert "USD 0.00" in entry["detail"] and "CNY -0.24" in entry["detail"]
    assert "insufficient" in entry["detail"].lower()  # is_available False from the producer


def test_deepseek_missing_or_invalid_balance_info_is_unavailable_not_zero():
    entry = _provider(_build(["deepseek"], deepseek={"is_available": True, "balance_infos": []}), "deepseek")
    assert entry["state"] == "unavailable" and "balance" not in entry and entry["windows"] == []

    invalid = {"is_available": True, "balance_infos": [{"currency": "USD", "total_balance": "not-a-number"}]}
    entry = _provider(_build(["deepseek"], deepseek=invalid), "deepseek")
    assert entry["state"] == "unavailable" and "balance" not in entry


def test_deepseek_without_credentials_is_unavailable_not_error():
    entry = _provider(_build(["deepseek"], deepseek=subscription_usage.SubscriptionUsageUnavailable()), "deepseek")
    assert entry["state"] == "unavailable"
    assert "credentials" in entry["detail"].lower()


def test_copilot_windows_follow_percent_remaining_and_dates():
    entry = _provider(_build(["copilot"], copilot=COPILOT_PAYLOAD), "copilot")
    assert entry["state"] == "available"
    assert entry["kind"] == "subscription"
    assert entry["plan"] == "Individual"
    labels = [w["label"] for w in entry["windows"]]
    assert labels == ["Chat", "Completions", "Premium requests"]
    remaining = {w["label"]: w["remaining_percent"] for w in entry["windows"]}
    # percent_remaining is ALREADY remaining — it must not be converted a second time.
    assert remaining == {"Chat": 100.0, "Completions": 100.0, "Premium requests": 36.3}
    assert entry["windows"][-1]["reset_at"] == "2026-11-01T00:00:00+00:00"


def test_copilot_derives_remaining_from_entitlement_when_percent_missing():
    payload = {
        "copilot_plan": "business",
        "quota_snapshots": {"premium_interactions": {"entitlement": 200, "quota_remaining": 50.0}},
    }
    entry = _provider(_build(["copilot"], copilot=payload), "copilot")
    (window,) = entry["windows"]
    assert window["remaining_percent"] == 25.0 and window["reset_at"] is None


def test_copilot_invalid_snapshots_are_skipped_not_zeroed():
    payload = {
        "quota_snapshots": {
            "chat": {"percent_remaining": None, "unlimited": True},
            "completions": {},
            "premium_interactions": {"percent_remaining": 104.9},
        }
    }
    entry = _provider(_build(["copilot"], copilot=payload), "copilot")
    labels = [w["label"] for w in entry["windows"]]
    assert labels == ["Premium requests"]  # only the finite one survives
    assert entry["windows"][0]["remaining_percent"] == 100.0  # clamped into 0..100


def test_fetch_failures_are_sanitized_and_never_leak_secrets():
    leak = "Bearer sk-verysecret-token-abc123"
    for provider_id, kwargs in (
        ("openai-codex", {"codex": RuntimeError(f"boom {leak}")}),
        ("deepseek", {"deepseek": RuntimeError(f"boom {leak}")}),
        ("copilot", {"copilot": RuntimeError(f"boom {leak}")}),
    ):
        doc = _build([provider_id], **kwargs)
        entry = _provider(doc, provider_id)
        assert entry["state"] == "error"
        assert entry["detail"] == subscription_usage._FETCH_FAILED_DETAIL
        serialized = json.dumps(doc)
        assert "sk-verysecret-token-abc123" not in serialized and "Bearer" not in serialized and "boom" not in serialized


def test_codex_credentials_use_the_read_only_resolver_without_refresh():
    calls = []

    def fake_resolve(**kwargs):
        calls.append(kwargs)
        return {"api_key": "stored-token", "base_url": "https://chatgpt.com/backend-api/codex", "source": "hermes-auth-store"}

    with patch("hermes_cli.auth_codex.resolve_codex_runtime_credentials", side_effect=fake_resolve), \
         patch("hermes_cli.auth._read_codex_tokens", return_value={"tokens": {}}), \
         patch.object(subscription_usage, "_http_get_json", return_value=CODEX_PAYLOAD) as http:
        subscription_usage._codex_usage_payload()

    assert calls == [{"read_only": True}], "quota queries must never refresh/replace stored credentials"
    # The request carried the stored token, and the resolver was not asked for any refresh knob.
    assert "refresh_if_expiring" not in calls[0] and "force_refresh" not in calls[0]
    assert http.call_args.args[1]["Authorization"] == "Bearer stored-token"


def test_local_and_unimplemented_providers_are_explicit_unavailable():
    doc = _build(["lmstudio", "commandcode", "ollama-cloud"])
    local = _provider(doc, "lmstudio")
    assert local["state"] == "unavailable" and local["kind"] == "local" and local["windows"] == []
    for provider_id in ("commandcode", "ollama-cloud"):
        entry = _provider(doc, provider_id)
        assert entry["state"] == "unavailable" and entry["kind"] == "subscription"
        assert "No quota endpoint" in entry["detail"] and entry["windows"] == []


# ── cycle 3: discovery, bounded deadline, human render, and the CLI --all flag ─────────────────


def test_discovery_uses_native_pool_auth_config_and_env_sources(monkeypatch):
    from hermes_cli import auth as auth_mod
    from hermes_cli import config as config_mod
    from hermes_cli import runtime_provider
    import providers as providers_mod

    class _Profile:
        def __init__(self, name, env_vars):
            self.name = name
            self.env_vars = env_vars

    monkeypatch.setattr(
        auth_mod,
        "read_credential_pool",
        lambda provider_id=None: {"acme-llm": [{"source": "manual:key"}], "openai-codex": [{"source": "device_code"}]},
    )
    monkeypatch.setattr(auth_mod, "_load_auth_store", lambda: {"providers": {"corp-oauth": {"tokens": {}}}})
    monkeypatch.setattr(runtime_provider, "resolve_requested_provider", lambda requested=None: "openai-codex")
    monkeypatch.setattr(
        config_mod,
        "load_config",
        lambda: {"providers": {"kimi-k3": {"base_url": "https://api.example", "key_env": "KIMI_K3_KEY"}, "custom": {"models": []}}},
    )
    monkeypatch.setattr(
        providers_mod,
        "list_providers",
        lambda: (
            _Profile("deepseek", ("DEEPSEEK_API_KEY",)),
            _Profile("anthropic", ("CLAUDE_CODE_OAUTH_TOKEN",)),
            _Profile("unused", ("UNUSED_KEY",)),
        ),
    )
    monkeypatch.setattr(
        config_mod,
        "get_env_value_prefer_dotenv",
        lambda name: {"DEEPSEEK_API_KEY": "sk-1234567890", "CLAUDE_CODE_OAUTH_TOKEN": "oauth-token-xyz"}.get(name, ""),
    )

    ids = subscription_usage._configured_provider_ids()
    assert ids[0] == "acme-llm"  # pool order is preserved
    assert {"acme-llm", "openai-codex", "corp-oauth", "kimi-k3", "deepseek"} <= set(ids)
    # Arbitrary pool/oauth ids surface; unconfigured registry profiles and settings buckets do not.
    assert "custom" not in ids and "unused" not in ids
    # CLAUDE_CODE_OAUTH_TOKEN is exported by Claude Code itself, not an explicit Hermes config.
    assert "anthropic" not in ids
    assert len(ids) == len(set(ids))


def test_arbitrary_pool_provider_renders_as_explicit_unavailable():
    doc = _build(["acme-llm"])
    entry = _provider(doc, "acme-llm")
    assert entry["state"] == "unavailable" and entry["windows"] == [] and entry["kind"] == "subscription"


def test_slow_collector_hits_deadline_without_blocking_the_others():
    def slow_codex():
        time.sleep(3)
        return CODEX_PAYLOAD

    started = time.monotonic()
    doc = _build(["openai-codex", "copilot"], codex=slow_codex, copilot=COPILOT_PAYLOAD, deadline_seconds=0.25)
    elapsed = time.monotonic() - started

    codex_entry = _provider(doc, "openai-codex")
    assert codex_entry["state"] == "error" and codex_entry["detail"] == subscription_usage._TIMED_OUT_DETAIL
    assert _provider(doc, "copilot")["state"] == "available"  # concurrency: the fast one still lands
    assert elapsed < 2.0  # the deadline bounds the document, not the abandoned worker


def test_render_subscription_usage_lines_covers_every_provider():
    doc = _build(["openai-codex", "deepseek", "lmstudio"], codex=CODEX_PAYLOAD, deepseek=DEEPSEEK_PAYLOAD)
    text = "\n".join(subscription_usage.render_subscription_usage_lines(doc))
    assert "Subscription usage" in text
    assert "44% remaining" in text  # codex weekly used→remaining
    assert "0.00 USD" in text  # deepseek balance
    assert "LM Studio" in text and "Local provider" in text


# ── CLI: hermes usage --all (desktop calls this through cli.exec) ──────────────────────────────


def _run_cli(argv):
    with patch.object(hermes_main, "_plugin_cli_discovery_needed", return_value=False), \
         patch.object(sys, "argv", ["hermes", *argv]):
        try:
            hermes_main.main()
        except SystemExit as exc:
            return int(exc.code or 0)
    return 0


def test_cli_usage_all_json_emits_the_panel_document(capsys):
    with patch.object(subscription_usage, "_configured_provider_ids", return_value=["openai-codex", "lmstudio"]), \
         patch.object(subscription_usage, "_codex_usage_payload", return_value=CODEX_PAYLOAD):
        code = _run_cli(["usage", "--all", "--json"])

    assert code == 0
    out, err = capsys.readouterr()
    doc = json.loads(out)
    assert err == ""
    assert doc["schema_version"] == 1
    assert [p["id"] for p in doc["providers"]] == ["openai-codex", "lmstudio"]
    assert doc["providers"][0]["windows"][0]["remaining_percent"] == 44.0


def test_cli_usage_all_without_data_still_exits_zero(capsys):
    with patch.object(subscription_usage, "_configured_provider_ids", return_value=["ollama-cloud"]):
        code = _run_cli(["usage", "--all", "--json"])

    assert code == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["providers"][0]["state"] == "unavailable"


def test_cli_usage_all_human_output_names_the_providers(capsys):
    with patch.object(subscription_usage, "_configured_provider_ids", return_value=["openai-codex"]), \
         patch.object(subscription_usage, "_codex_usage_payload", return_value=CODEX_PAYLOAD):
        code = _run_cli(["usage", "--all"])

    assert code == 0
    out = capsys.readouterr().out
    assert "Subscription usage" in out and "44% remaining" in out


def test_cli_usage_all_wins_over_provider_flag(capsys):
    with patch.object(subscription_usage, "_configured_provider_ids", return_value=["openai-codex"]), \
         patch.object(subscription_usage, "_codex_usage_payload", return_value=CODEX_PAYLOAD):
        code = _run_cli(["usage", "--all", "--json", "--provider", "deepseek"])

    assert code == 0
    doc = json.loads(capsys.readouterr().out)
    assert [p["id"] for p in doc["providers"]] == ["openai-codex"]


def test_cli_usage_without_all_keeps_the_legacy_single_provider_document(capsys):
    legacy = AccountUsageSnapshot(
        provider="openai-codex", source="usage_api", fetched_at=datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc),
        windows=(AccountUsageWindow(label="Session", used_percent=37.0),),
    )
    with patch("agent.account_usage.fetch_account_usage", lambda provider, **kw: legacy):
        code = _run_cli(["usage", "--json", "--provider", "openai-codex"])

    assert code == 0
    doc = json.loads(capsys.readouterr().out)
    assert "schema_version" not in doc  # legacy document untouched
    assert doc["provider"] == "openai-codex" and doc["windows"][0]["label"] == "Session"
