"""Canonical reasoning-effort vocabulary and wire clamping.

Hermes' internal effort ladder (``hermes_constants.VALID_REASONING_EFFORTS``
plus the ``none`` disable level) is wider than what any single provider wire
accepts. Historically every transport and provider profile hand-rolled its own
translation map, and the class of bugs that produced was constant: a new
internal level (``ultra``) leaking to a wire that rejects it with HTTP 400
(#89503, #70058), or an unknown level being dropped to a weak default so the
strongest ask resolved *weaker* than an explicit ``high`` — a ladder
inversion (#74295, #87279).

This module is the single source of truth both kinds of code use instead:

- :data:`EFFORT_LADDER` — canonical low→high ordering.
- :func:`clamp_effort` — the one clamping policy: keep a supported level
  verbatim, otherwise take the **nearest weaker** supported level (never
  silently escalate cost above what was asked), and only when nothing weaker
  exists take the weakest supported level (a provider whose minimum thinking
  level is ``high`` serves ``high`` for a ``low`` ask — GLM-5.2's shape).
- Named wire-vocabulary constants for the common OpenAI-compatible surfaces,
  so call sites declare *data* ("this route accepts these levels") rather
  than logic.

Rules for call sites:

1. **Wire shape stays local.** Whether a route wants ``extra_body.reasoning``,
   a top-level ``reasoning_effort`` string, or a ``thinking`` toggle is the
   caller's business. Only the *vocabulary math* lives here.
2. **Unset stays unset.** ``clamp_effort`` translates an explicit request; it
   does not invent one. When the user expressed no effort, prefer omitting
   the field so the server default applies.
3. **Never patch a predicate.** When a provider rejects a level, fix its
   declared supported set (data), never add another vendor-name special case
   at the call site.
"""

from __future__ import annotations

import re
from typing import Optional, Sequence

#: K3 slug detector — matches ``k3`` as a delimited token (``k3``,
#: ``k3-256k``, ``kimi-k3``, ``kimi-k3-cot``) without matching K2-era names
#: (``kimi-k2.6``). From #76427 by @ruizanthony.
_KIMI_K3_SLUG_RE = re.compile(r"(?:^|[^a-z0-9])k3(?:[^a-z0-9]|$)")

# Canonical low→high ordering used for nearest-level clamping. Superset of
# hermes_constants.VALID_REASONING_EFFORTS ("none" included so an explicit
# disable can be clamped too when a provider publishes it as a level).
EFFORT_LADDER: tuple[str, ...] = (
    "none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra",
)

# ``ultra`` is Hermes-internal ladder vocabulary (the Codex product tier); no
# provider wire accepts it verbatim anywhere. Every declared wire set below
# therefore stops at ``max`` — ``ultra`` always clamps down.

#: The widest OpenAI-compatible wire vocabulary (OpenRouter, Nous Portal):
#: exactly max|xhigh|high|medium|low|minimal|none.
OPENAI_COMPAT_WIRE_EFFORTS: tuple[str, ...] = (
    "none", "minimal", "low", "medium", "high", "xhigh", "max",
)

#: Levels a *user* can be offered for a route: the ladder minus the
#: disable-only level, which is a thinking switch rather than an effort.
SELECTABLE_EFFORT_LEVELS: tuple[str, ...] = tuple(
    level for level in EFFORT_LADDER if level != "none"
)

#: ``api_mode`` values that speak the OpenAI/Codex Responses wire, whose
#: effort vocabulary is per-model rather than one wide OpenAI-compatible set.
RESPONSES_API_MODES: tuple[str, ...] = ("codex_responses", "responses")

#: ``api_mode`` values that speak the Anthropic Messages wire, where the
#: effort dial is ``output_config.effort`` on adaptive-thinking models.
ANTHROPIC_API_MODES: tuple[str, ...] = (
    "anthropic_messages",
    "anthropic",
    "messages",
)

#: OpenAI/Codex Responses backend — per-model vocabulary, live-verified
#: (Aug 2026): ``minimal`` is rejected by both generations (clamps to low);
#: ``max`` is gpt-5.6-only — gpt-5.5 rejects it with "Supported values are:
#: 'none', 'low', 'medium', 'high', 'xhigh'" (#68365's premise, confirmed).
CODEX_GPT56_EFFORTS: tuple[str, ...] = (
    "none", "low", "medium", "high", "xhigh", "max",
)
CODEX_LEGACY_EFFORTS: tuple[str, ...] = (
    "none", "low", "medium", "high", "xhigh",
)


def codex_supported_efforts(model: Optional[str]) -> tuple[str, ...]:
    """Supported effort set for an OpenAI/Codex Responses model."""
    if "gpt-5.6" in (model or "").lower():
        return CODEX_GPT56_EFFORTS
    return CODEX_LEGACY_EFFORTS


#: Backward-compat alias (pre-#68365-verification name).
CODEX_RESPONSES_EFFORTS: tuple[str, ...] = CODEX_GPT56_EFFORTS

#: xAI Responses — Grok 4.6+ accepts xhigh; older Grok tops out at high.
XAI_GROK46_EFFORTS: tuple[str, ...] = ("low", "medium", "high", "xhigh")
XAI_LEGACY_EFFORTS: tuple[str, ...] = ("low", "medium", "high")

#: Actual Computer relays (SGLang/vLLM): none/low/medium/high/max.
ACTUAL_RELAY_EFFORTS: tuple[str, ...] = ("none", "low", "medium", "high", "max")

#: Moonshot/Kimi K3: low/high/max (server default high).
KIMI_K3_EFFORTS: tuple[str, ...] = ("low", "high", "max")
#: Moonshot/Kimi K2-era models: low/medium/high.
KIMI_K2_EFFORTS: tuple[str, ...] = ("low", "medium", "high")

#: OpenCode "Ox Alpha" stealth model (x-preview-f-free): thinking is always
#: on and the wire accepts exactly low/high/max — medium/none/xhigh 400 with
#: "This model always engages in thinking and cannot be disabled; please use
#: low, high, or max" (verified live 2026-08-21). xhigh rounds up to max.
OX_ALPHA_EFFORTS: tuple[str, ...] = ("low", "high", "max")
OX_ALPHA_OVERRIDES: dict[str, str] = {"xhigh": "max"}

#: Tencent TokenHub: low/medium/high.
TOKENHUB_EFFORTS: tuple[str, ...] = ("low", "medium", "high")

#: Nebius Token Factory: low/medium/high (top-level reasoning_effort knob).
NEBIUS_EFFORTS: tuple[str, ...] = ("low", "medium", "high")

#: Kimi K3's vendor-documented translation quirks (platform.kimi.ai
#: thinking-model guide): ``high`` is K3's positional middle AND server
#: default, so ``medium`` rounds to it rather than down to ``low``; ``xhigh``
#: rounds up to ``max`` (K3's top tier), matching the kimi-coding plugin.
KIMI_K3_OVERRIDES: dict[str, str] = {"medium": "high", "xhigh": "max"}

#: GLM-5.2 native reasoning_effort knob: exactly two enabled levels,
#: ``high`` (its minimum thinking level) and ``max`` (per Z.AI/BigModel
#: docs). ``xhigh`` requests the top tier, not the floor.
GLM52_EFFORTS: tuple[str, ...] = ("high", "max")
GLM52_OVERRIDES: dict[str, str] = {"xhigh": "max"}

#: GLM-5.3 widens the knob to a graded low/medium/high/max scale — verified
#: live on api.z.ai/api/coding/paas/v4 (issue #91789, 2026-08-21): every
#: level accepted with monotonic reasoning-token scaling (low=4, medium=11,
#: high=98, max=125 on the probe prompt). ``xhigh`` requests the top tier.
GLM53_EFFORTS: tuple[str, ...] = ("low", "medium", "high", "max")
GLM53_OVERRIDES: dict[str, str] = {"xhigh": "max"}

#: DeepSeek V4 OpenAI-compat endpoint: low/medium/high/max; ``xhigh``
#: requests the top tier (matches the shipped profile mapping).
DEEPSEEK_V4_EFFORTS: tuple[str, ...] = ("low", "medium", "high", "max")
DEEPSEEK_V4_OVERRIDES: dict[str, str] = {"xhigh": "max"}

#: Ollama Cloud /v1/chat/completions: accepts {none, low, medium, high, max};
#: rejects ``minimal`` with HTTP 400. ``xhigh`` requests the top tier.
OLLAMA_CLOUD_EFFORTS: tuple[str, ...] = ("none", "low", "medium", "high", "max")
OLLAMA_CLOUD_OVERRIDES: dict[str, str] = {"xhigh": "max"}

#: Meta Model API (Muse): minimal..xhigh; rejects ``none``.
META_AI_EFFORTS: tuple[str, ...] = ("minimal", "low", "medium", "high", "xhigh")

#: Upstage Solar Pro/Open: low/medium/high.
SOLAR_EFFORTS: tuple[str, ...] = ("low", "medium", "high")


def kimi_supported_efforts(model: Optional[str]) -> tuple[str, ...]:
    """Supported effort set for a Moonshot/Kimi model slug.

    K3 is served as the bare slug ``k3``, plan variants like ``k3-256k``,
    and the ``kimi-k3*`` aliases; its documented set is low/high/max.
    Everything earlier speaks low/medium/high. Boundary-matched so K2-era
    names (``kimi-k2.6``) never match (detection regex from #76427 by
    @ruizanthony).
    """
    m = (model or "").strip().lower().split("/")[-1]
    if _KIMI_K3_SLUG_RE.search(m):
        return KIMI_K3_EFFORTS
    return KIMI_K2_EFFORTS


def clamp_effort(
    effort: Optional[str],
    supported: Optional[Sequence[str]],
    overrides: Optional[dict[str, str]] = None,
) -> Optional[str]:
    """Clamp a requested reasoning effort onto a wire's supported levels.

    ``overrides`` is an optional declared mapping consulted first, for routes
    whose vendor documents a translation that differs from nearest-weaker
    (Kimi K3 documents ``medium → high``: high is its positional middle and
    server default). Overrides are data, not logic — a call site never adds
    vendor ``if``\\ s around this function.

    Otherwise: returns the requested effort unchanged when it is supported,
    when the supported set is unknown (``None``/empty), or when the effort
    isn't a recognized ladder level (custom providers may use bespoke names —
    pass through rather than guess). Otherwise returns the **nearest weaker**
    supported level, so a clamp never silently escalates cost; when nothing
    weaker exists, the weakest supported level is returned (the caller asked
    for *some* thinking and the provider's floor is the closest honest match).

    The policy is monotonic: a stronger request never resolves to a weaker
    wire level than a weaker request would.
    """
    requested = str(effort or "").strip().lower()
    if not requested or not supported:
        return effort
    supported_norm = [
        str(level).strip().lower()
        for level in supported
        if str(level).strip().lower() in EFFORT_LADDER
    ]
    if not supported_norm or requested in supported_norm:
        return effort
    if overrides:
        mapped = overrides.get(requested)
        if mapped in supported_norm:
            return mapped
    if requested not in EFFORT_LADDER:
        return effort
    # "none" disables reasoning — it is never a *degradation target* for an
    # enabled ask (clamping "minimal" to "none" would silently switch
    # thinking off). It still passes through verbatim when requested.
    candidates = [level for level in supported_norm if level != "none"]
    if not candidates:
        return effort
    requested_idx = EFFORT_LADDER.index(requested)
    below = [
        level for level in candidates
        if EFFORT_LADDER.index(level) < requested_idx
    ]
    if below:
        return max(below, key=EFFORT_LADDER.index)
    return min(candidates, key=EFFORT_LADDER.index)


def requested_effort(reasoning_config: Optional[dict]) -> Optional[str]:
    """Extract the user's explicit effort from a reasoning config, or None.

    Returns ``None`` when the config is absent, malformed, carries no effort,
    or reasoning is explicitly disabled — callers should then omit the wire
    field entirely so the server default applies (rule 2 above).
    """
    if not isinstance(reasoning_config, dict):
        return None
    if reasoning_config.get("enabled") is False:
        return None
    effort = str(reasoning_config.get("effort") or "").strip().lower()
    return effort or None


# ── Capability read side ─────────────────────────────────────────────
#
# Everything below answers "which effort levels can this route be asked
# for?" from the SAME declarations the request path already clamps onto, so
# a picker never has to guess from a model name. Two callers consume it:
# the model-options capability map (``hermes_cli.inventory``) and the
# session-scoped ``config.get key=reasoning`` answer (``tui_gateway``).


def selectable_efforts(levels: Optional[Sequence[str]]) -> tuple[str, ...]:
    """Ladder-ordered levels from *levels* that a user can be offered.

    Drops the disable-only ``none`` (a thinking switch, not an effort) and
    keeps any bespoke names a custom provider declares, in declared order
    after the recognized ladder levels. ``None`` stays ``None`` (undeclared),
    an empty sequence stays empty (the route takes no reasoning at all).
    """
    if levels is None:
        return ()
    seen: list[str] = []
    bespoke: list[str] = []
    for level in levels:
        name = str(level or "").strip().lower()
        if not name or name == "none" or name in seen or name in bespoke:
            continue
        if name in EFFORT_LADDER:
            seen.append(name)
        else:
            bespoke.append(name)
    ordered = sorted(seen, key=EFFORT_LADDER.index)
    return tuple(ordered + bespoke)


def profile_declared_efforts(
    provider: Optional[str], model: Optional[str], base_url: Any = None
) -> Optional[tuple[str, ...]]:
    """Provider-profile-declared effort vocabulary for *model*, or ``None``.

    Tri-state, mirroring ``ProviderProfile.supported_reasoning_efforts``:
    ``None`` = undeclared, ``()`` = the route takes no reasoning parameter,
    non-empty = the levels it validates. Resolution is by provider name
    first, then by the endpoint host (a named custom provider pointed at a
    known provider's endpoint gets that provider's vocabulary too — the
    host, not the config-entry name, is what validates the request).
    """
    try:
        from providers import get_provider_profile

        name = str(provider or "").strip().lower()
        profile = get_provider_profile(name) if name else None
        declared = (
            profile.supported_reasoning_efforts(model) if profile is not None else None
        )
        if declared is None and base_url:
            from agent.model_metadata import _infer_provider_from_url

            inferred = _infer_provider_from_url(str(base_url))
            if inferred and inferred != name:
                inferred_profile = get_provider_profile(inferred)
                if inferred_profile is not None:
                    declared = inferred_profile.supported_reasoning_efforts(model)
    except Exception:
        # Fail-open by design: a broken profile hook must never block a
        # request or a picker — the route default below still applies.
        return None
    if declared is None:
        return None
    return tuple(declared)


def profile_declared_overrides(
    provider: Optional[str], model: Optional[str], base_url: Any = None
) -> dict[str, str]:
    """Provider-declared requested→wire translations for *model* (may be empty)."""
    try:
        from providers import get_provider_profile

        name = str(provider or "").strip().lower()
        profile = get_provider_profile(name) if name else None
        declared = (
            profile.reasoning_effort_overrides(model) if profile is not None else None
        )
        if declared is None and base_url:
            from agent.model_metadata import _infer_provider_from_url

            inferred = _infer_provider_from_url(str(base_url))
            if inferred and inferred != name:
                inferred_profile = get_provider_profile(inferred)
                if inferred_profile is not None:
                    declared = inferred_profile.reasoning_effort_overrides(model)
    except Exception:
        return {}
    if not isinstance(declared, dict):
        return {}
    return {
        str(k).strip().lower(): str(v).strip().lower()
        for k, v in declared.items()
        if str(k).strip() and str(v).strip()
    }


def _anthropic_route_efforts(model: Optional[str]) -> tuple[str, ...]:
    """Adaptive-thinking effort levels Anthropic's wire accepts for *model*."""
    try:
        from agent.anthropic_adapter import adaptive_effort_levels

        levels = adaptive_effort_levels(model)
        if levels:
            return tuple(levels)
    except Exception:
        pass
    # Fail-open to the documented adaptive contract rather than under-report.
    return ("low", "medium", "high", "max")


def route_api_mode(
    provider: Optional[str], api_mode: Optional[str] = None, base_url: Any = None
) -> str:
    """The wire mode a route speaks: explicit, else the provider profile's."""
    mode = str(api_mode or "").strip().lower()
    if mode:
        return mode
    try:
        from providers import get_provider_profile

        profile = get_provider_profile(str(provider or "").strip().lower())
        if profile is not None:
            return str(getattr(profile, "api_mode", "") or "").strip().lower()
    except Exception:
        pass
    return ""


def supported_efforts_for_route(
    provider: Optional[str] = None,
    model: Optional[str] = None,
    api_mode: Optional[str] = None,
    base_url: Any = None,
) -> tuple[str, ...]:
    """Effort levels *this route* can be asked for, in ladder order.

    The single read side of a single declaration chain:

    1. the provider profile's own declared vocabulary (authoritative when
       present — it is what the profile's request path validates against),
       ``()`` meaning "this route takes no reasoning parameter at all";
    2. otherwise the transport default for the route's wire: the per-model
       OpenAI/Codex Responses vocabulary, the Anthropic adaptive set, or the
       wide OpenAI-compatible set — exactly what the transport would clamp
       onto when no profile narrows it.

    Returns a ladder-ordered tuple of selectable levels (possibly empty),
    never ``None``: callers showing a control need a concrete answer.
    """
    declared = profile_declared_efforts(provider, model, base_url)
    if declared is not None:
        return selectable_efforts(declared)
    mode = route_api_mode(provider, api_mode, base_url)
    if mode in RESPONSES_API_MODES:
        return selectable_efforts(codex_supported_efforts(model))
    if mode in ANTHROPIC_API_MODES:
        return selectable_efforts(_anthropic_route_efforts(model))
    return selectable_efforts(OPENAI_COMPAT_WIRE_EFFORTS)


def effective_effort(
    effort: Optional[str],
    supported: Optional[Sequence[str]],
    overrides: Optional[dict[str, str]] = None,
) -> str:
    """The level this route will actually be asked for, given *effort*.

    ``clamp_effort`` with the route's declared overrides, coerced to a string
    so a client can always render it. An unset effort stays unset (``""``):
    the provider's own server default applies and we do not invent one.
    """
    requested = str(effort or "").strip().lower()
    if not requested:
        return ""
    clamped = clamp_effort(requested, supported, overrides)
    return str(clamped or "").strip().lower()


def reasoning_capability(
    provider: Optional[str] = None,
    model: Optional[str] = None,
    api_mode: Optional[str] = None,
    base_url: Any = None,
    effort: Optional[str] = None,
) -> dict:
    """Normalized reasoning capability + effective value for one route.

    The shape a client renders from — nothing here is model-name inference:

        {"supported": True, "values": ["low", "medium", "high", "xhigh"],
         "can_disable": True, "value": "xhigh", "effective": "xhigh"}

    ``supported`` is False with an empty ``values`` when the route declares
    no reasoning parameter (``()`` from the profile). ``value`` is what the
    user asked for (verbatim, or ``""`` when nothing was picked: the provider
    default applies); ``effective`` is Hermes' own translation of it onto
    ``values`` — the level that will reach the wire. ``can_disable`` reports
    whether an explicit thinking-off is a known-accepted request; ``None``
    when no catalog says, which a client must read as "no restriction known".
    """
    values = supported_efforts_for_route(provider, model, api_mode, base_url)
    overrides = (
        profile_declared_overrides(provider, model, base_url) if values else {}
    )
    requested = requested_effort({"effort": effort}) if effort else ""
    return {
        "supported": bool(values),
        "values": list(values),
        "can_disable": None if values else False,
        "value": str(requested or ""),
        "effective": effective_effort(requested, values, overrides),
    }
