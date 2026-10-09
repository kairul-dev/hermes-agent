"""Regression for independent Desktop/Forge catalog cache writers.

Catalog ids and the incompatible-writer shape match the observed local defect.
Only network/credential boundaries are stubbed; persistence uses real JSON files.
"""
import json
from unittest.mock import patch

import hermes_cli.models as mod


def test_desktop_catalog_survives_legacy_forge_cache_overwrite(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    live_models = ['gpt-6.1-sol', 'gpt-6-astra']
    with patch.object(mod, '_credential_fingerprint', return_value='desktop-principal'), \
         patch.object(mod, '_spawn_swr_refresh') as refresh, \
         patch.object(mod, 'provider_model_ids') as network:
        mod.update_provider_cache_entry('openai-codex', live_models)
        # Forge writes the old schema/path after Desktop has warmed its own catalog.
        (tmp_path / 'provider_models_cache.json').write_text(json.dumps({
            'openai-codex': mod._cache_entry('forge-incompatible-key', ['gpt-6-sol']),
        }), encoding='utf-8')
        actual = mod.cached_provider_model_ids('openai-codex', non_blocking=True)
        assert actual == live_models
        refresh.assert_not_called()
        network.assert_not_called()
