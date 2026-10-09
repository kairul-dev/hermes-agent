"""Bounded launch/ABI handoff regressions; no interpreter or tool is installed."""
from collections import namedtuple
import json
from pathlib import Path
import sys

import pytest

import pm
from hermes_cli import _launchers, venv_sync
from pm import environments


_VERSION = namedtuple("Version", "major minor micro releaselevel serial")


@pytest.fixture
def launch_install(tmp_path, monkeypatch):
    # HERMES_HOME alone is not enough beneath the native Hermes data root.
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "native-root"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("HERMES_DISABLE_LAZY_INSTALLS", raising=False)
    monkeypatch.delenv("HERMES_SUPERVISED_CHILD", raising=False)
    monkeypatch.delenv("HERMES_S6_SUPERVISED_CHILD", raising=False)
    root = tmp_path / "checkout"
    root.mkdir()
    (root / ".git").mkdir()
    (root / "pyproject.toml").write_text("[project]\nname='example'\n", encoding="utf-8")
    stamp = root / "install-stamp.json"
    stamp.write_text(json.dumps({"updateMechanism": "self"}), encoding="utf-8")
    state = environments.install_state_dir(root)
    assert state.resolve().is_relative_to(tmp_path.resolve()), state
    versions = {}

    def generation(name, version):
        venv = state / "environments" / name / "venv"
        python = environments.venv_python(venv)
        python.parent.mkdir(parents=True)
        python.touch()
        (venv / "pyvenv.cfg").write_text(f"version_info = {version}.0\n", encoding="utf-8")
        environments.site_packages(venv).mkdir(parents=True)
        versions[python] = tuple(map(int, version.split(".")))
        return venv, python

    selected, selected_python = generation("selected", "3.14")
    replacement, replacement_python = generation("replacement", "3.14")
    store = tmp_path / "store" / "python"
    store.parent.mkdir()
    store.touch()
    legacy = tmp_path / "legacy" / "python"
    legacy.parent.mkdir()
    legacy.touch()
    versions.update({store: (3, 15), legacy: (3, 11)})

    def select(venv):
        environments.runtime_facts_path(root).write_text(json.dumps({
            "packages": {"venv": {"environment": str(venv)}}
        }), encoding="utf-8")

    select(selected)
    monkeypatch.setattr(_launchers, "resolve_store_python", lambda _: store)
    monkeypatch.setattr(sys, "argv", [str(root / "entry.py"), "doctor"])
    return root, selected_python, replacement, replacement_python, store, legacy, versions, select


@pytest.mark.parametrize("start", ["legacy", "store", "selected"])
@pytest.mark.parametrize("obligation", ["current", "pending-tail", "stale-sync"])
def test_launch_and_abi_handoff_converge_without_dropping_completion(
    launch_install, monkeypatch, start, obligation
):
    """Model at most four boots through BOTH real decisions, never spawn a loop."""
    root, selected, replacement, replacement_python, store, legacy, versions, select = launch_install
    ready = obligation != "stale-sync"
    syncs, tails, published = [], [], []
    monkeypatch.setattr(pm, "venv_is_current", lambda **kw: ready)
    monkeypatch.setattr(venv_sync, "_tree_matches_completed_stamp", lambda _: False)
    monkeypatch.setattr(venv_sync, "collect_superseded_generations", lambda _: None)
    monkeypatch.setattr(venv_sync, "publish_launchers", lambda root: published.append(root))
    from pm import client
    monkeypatch.setattr(client, "ensure_tools_for_sync", lambda: None)

    def sync(*args, **kwargs):
        nonlocal ready
        syncs.append(kwargs)
        # A sync can replace the committed generation: choose AFTER completion.
        select(replacement)
        ready = True

    monkeypatch.setattr(pm, "sync_venv", sync)
    monkeypatch.setattr(venv_sync.subprocess, "call", lambda command, **kw: tails.append(command) or 0)
    pending = venv_sync.completion_pending_path(root)
    if obligation == "pending-tail":
        venv_sync.arm_completion(root)
    python = {"legacy": legacy, "store": store, "selected": selected}[start]
    target = replacement_python if obligation == "stale-sync" else selected
    before = environments.runtime_facts_path(root).read_bytes()
    trace = []
    for _ in range(4):
        version = versions[python]
        monkeypatch.setattr(sys, "executable", str(python))
        monkeypatch.setattr(sys, "version_info", _VERSION(*version, 0, "final", 0))
        # Same order as hermes_bootstrap; a launch re-exec precedes ABI checking.
        launch = venv_sync.prepare_launch(root, ["doctor"])
        abi = environments.dependency_relaunch_python(root) if launch is None else None
        trace.append((python, launch, abi))
        if (next_python := launch or abi) is None:
            break
        python = next_python
    print("bounded launch trace:", trace)
    assert trace[-1][1:] == (None, None), f"launch/ABI decisions did not converge: {trace}"
    assert python == target, f"did not retain the committed interpreter: {trace}"
    assert len(trace) <= 2, f"unnecessary store hop before selected ABI: {trace}"
    assert len(syncs) == (1 if obligation == "stale-sync" else 0)
    assert len(tails) == (0 if obligation == "current" else 1)
    assert not pending.exists(), "required completion was suppressed"
    if obligation != "stale-sync":
        assert environments.runtime_facts_path(root).read_bytes() == before


@pytest.mark.parametrize("policy", ["metadata", "lazy-disabled", "developer", "pm", "tail"])
def test_launch_ownership_gates_remain_independent_of_read_only_abi_handoff(
    launch_install, monkeypatch, policy
):
    root, selected, _, _, store, _, versions, _ = launch_install
    argv = ["doctor"]
    if policy == "metadata":
        argv = ["--version"]
    elif policy == "lazy-disabled":
        monkeypatch.setenv("HERMES_DISABLE_LAZY_INSTALLS", "1")
    elif policy == "developer":
        (root / "install-stamp.json").write_text(json.dumps({"updateMechanism": "external"}), encoding="utf-8")
    elif policy == "pm":
        argv = ["-p", "test", "pm", "status"]
    elif policy == "tail":
        monkeypatch.setattr(sys, "argv", [str(root / "hermes_cli" / "source_completion.py")])
    monkeypatch.setattr(sys, "executable", str(store))
    monkeypatch.setattr(sys, "version_info", _VERSION(*versions[store], 0, "final", 0))
    monkeypatch.setattr(pm, "venv_is_current", lambda **kw: pytest.fail("ownership gate reached sync decision"))
    monkeypatch.setattr(venv_sync, "publish_launchers", lambda _: pytest.fail("ownership gate published launchers"))
    pending = venv_sync.arm_completion(root)
    before = (environments.runtime_facts_path(root).read_bytes(), pending.read_bytes(), sys.path[:])
    assert venv_sync.prepare_launch(root, argv) is None
    assert environments.dependency_relaunch_python(root) == selected
    monkeypatch.setattr(sys, "executable", str(selected))
    monkeypatch.setattr(sys, "version_info", _VERSION(3, 14, 0, "final", 0))
    assert venv_sync.prepare_launch(root, argv) is None
    assert environments.dependency_relaunch_python(root) is None
    assert (environments.runtime_facts_path(root).read_bytes(), pending.read_bytes(), sys.path) == before
