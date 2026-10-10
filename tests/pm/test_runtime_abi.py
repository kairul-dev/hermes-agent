"""Portable PM/boot ABI guard regression tests; no dependencies are installed."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from pm import environments


@pytest.fixture
def selected(tmp_path, monkeypatch):
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path/'native-root'))
    monkeypatch.setenv('HERMES_HOME', str(tmp_path/'home'))
    monkeypatch.setenv('HERMES_DISABLE_LAZY_INSTALLS','1')
    root = tmp_path/'repo'
    root.mkdir()
    state = environments.install_state_dir(root)
    assert state.resolve().is_relative_to(tmp_path.resolve()), state
    venv = state/'environments'/'fixture'/'venv'
    venv.mkdir(parents=True)
    python = environments.venv_python(venv)
    python.parent.mkdir()
    python.touch()
    (venv/'pyvenv.cfg').write_text(f'version_info = {sys.version_info.major}.{sys.version_info.minor + 1}.0\n',encoding='utf-8')
    environments.site_packages(venv).mkdir(parents=True)
    environments.runtime_facts_path(root).write_text(json.dumps({'packages': {'venv': {'environment': str(venv)}}}),encoding='utf-8')
    return root, venv, python


@pytest.mark.parametrize('key',['version','version_info'])
def test_cfg_reports_selected_version(selected,key):
    _, venv, _ = selected
    (venv/'pyvenv.cfg').write_text(f'{key} = 3.14.7\n',encoding='utf-8')
    assert environments.venv_python_version(venv) == (3,14)


def test_abi_handoff_is_read_only(selected):
    root, _, python = selected
    before = (sys.path[:],dict(os.environ))
    assert environments.dependency_relaunch_python(root) == python
    assert (sys.path,dict(os.environ)) == before


def test_matching_interpreter_retains_owner(selected):
    root,venv,_ = selected
    (venv/'pyvenv.cfg').write_text(f'version_info = {sys.version_info.major}.{sys.version_info.minor}.99\n',encoding='utf-8')
    assert environments.dependency_relaunch_python(root) is None


def test_unknown_version_preserves_legacy_contract(selected):
    root,venv,_ = selected
    # Unknown metadata also requires no version-bearing POSIX directory fallback.
    for directory in (venv/'lib').glob('python3*'):
        (directory/'site-packages').rmdir()
        directory.rmdir()
    (venv/'pyvenv.cfg').write_text('home = fixture\n',encoding='utf-8')
    assert environments.dependency_relaunch_python(root) is None


def test_unrecorded_environment_preserves_developer_interpreter(selected):
    root,_,_ = selected
    environments.runtime_facts_path(root).unlink()
    assert environments.dependency_relaunch_python(root) is None


def test_missing_selected_interpreter_fails_closed(selected):
    root,_,python = selected
    python.unlink()
    with pytest.raises(RuntimeError,match='interpreter is missing'):
        environments.dependency_relaunch_python(root)


def test_same_executable_with_wrong_version_does_not_relaunch_forever(selected,monkeypatch):
    root,_,python = selected
    monkeypatch.setattr(sys,'executable',str(python))
    with pytest.raises(RuntimeError,match='reports a different version'):
        environments.dependency_relaunch_python(root)


def test_direct_activation_rejects_foreign_abi_before_pth(selected):
    root,venv,_ = selected
    marker = root/'pth-executed'
    (environments.site_packages(venv)/'malicious.pth').write_text(f'import pathlib; pathlib.Path({str(marker)!r}).touch()\n',encoding='utf-8')
    before = (sys.path[:],dict(os.environ))
    with pytest.raises(RuntimeError,match='relaunch with'):
        environments.activate_dependencies(root)
    assert (sys.path,dict(os.environ)) == before
    assert not marker.exists()


def test_sealed_payload_handoff(tmp_path,monkeypatch):
    monkeypatch.setenv('LOCALAPPDATA',str(tmp_path/'native'))
    monkeypatch.setenv('HERMES_HOME',str(tmp_path/'home'))
    payload = tmp_path/'payload'
    root = payload/'repo'
    root.mkdir(parents=True)
    venv = payload/'venv'
    python = environments.venv_python(venv)
    python.parent.mkdir(parents=True)
    python.touch()
    (venv/'pyvenv.cfg').write_text(f'version_info = {sys.version_info.major}.{sys.version_info.minor+1}.0\n',encoding='utf-8')
    (payload/'manifest.json').write_text(json.dumps({'repo':'repo','venv':'venv'}),encoding='utf-8')
    assert environments.dependency_relaunch_python(root) == python


@pytest.mark.parametrize('args',[['pm','repair'],['-p','james','pm','repair']])
def test_explicit_repair_bypasses_wrong_abi(selected,args):
    _,venv,_ = selected
    source = Path(environments.__file__).resolve().parents[1]
    # Bind fixture selection to the actual test snapshot root, not live code.
    record = environments.runtime_facts_path(source)
    dest = record.parent/'environments'/'fixture'/'venv'
    import shutil
    shutil.copytree(venv,dest)
    record.parent.mkdir(parents=True,exist_ok=True)
    record.write_text(json.dumps({'packages': {'venv': {'environment': str(dest)}}}),encoding='utf-8')
    code = 'import sys; sys.path.insert(0,sys.argv[1]); sys.argv=["hermes",*sys.argv[2:]]; import hermes_bootstrap; print("repair-ready")'
    result = subprocess.run([sys.executable,'-I','-B','-c',code,str(source),*args],capture_output=True,text=True,env=dict(os.environ),timeout=30)
    assert result.returncode == 0,result.stderr
    assert result.stdout.strip() == 'repair-ready'
