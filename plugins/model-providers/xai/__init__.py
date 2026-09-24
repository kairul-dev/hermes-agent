"""xAI (Grok) provider profile."""

from hermes_cli import __version__ as _HERMES_VERSION
from providers import register_provider
from providers.base import ProviderProfile


class XaiProfile(ProviderProfile):
    """xAI — Grok on the OpenAI/Codex Responses wire."""

    def supported_reasoning_efforts(self, model: str | None) -> tuple[str, ...]:
        """Grok's ``reasoning.effort`` ladder, per family.

        Grok 4.6 accepts ``xhigh``; older Grok tops out at ``high``. These are
        the two sets the Responses transport clamps onto for this provider
        (``agent.transports.codex``), so a picker offering them offers exactly
        what Hermes will put on the wire.
        """
        from agent.model_metadata import is_grok_46_family
        from agent.reasoning_effort import XAI_GROK46_EFFORTS, XAI_LEGACY_EFFORTS

        return XAI_GROK46_EFFORTS if is_grok_46_family(model or "") else XAI_LEGACY_EFFORTS


xai = XaiProfile(
    name="xai",
    aliases=("grok", "x-ai", "x.ai"),
    api_mode="codex_responses",
    env_vars=("XAI_API_KEY",),
    base_url="https://api.x.ai/v1",
    auth_type="api_key",
    default_headers={"User-Agent": f"Hermes-Agent/{_HERMES_VERSION}"},
)

register_provider(xai)
