"""Real subprocess evidence for the synthetic community harness."""
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('creator_simulation', ROOT/'experiments/creator_simulation/run.py')
sim = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sim)
ARTIFACT = ROOT/'experiments/creator_simulation/artifacts/creator_probe.idcap'


def test_cross_user_lifecycle_and_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(sim, 'ACTORS', [
        ('poet','identity_creator','poetry editor'),
        ('newsletter','capability_creator','newsletter formatting'),
        ('reader','consumer','reading poetry'),
    ])
    summary = sim.run(tmp_path/'community', ARTIFACT)
    assert summary['failures'] == []
    ledger=[json.loads(line) for line in (tmp_path/'community/ledger.jsonl').read_text().splitlines()]
    reader=[r for r in ledger if r['actor']=='reader']
    install=next(r for r in reader if r['action']=='install_engineer_capability')
    invoke=next(r for r in reader if r['action']=='invoke_after_process_restart')
    assert json.loads(install['stdout'])['observed']['capability_id'] == 'newsletter_probe'
    assert json.loads(invoke['stdout'])['observed']['capability_id'] == 'newsletter_probe'
    assert json.loads(install['stdout'])['observed']['permission_denied_before_grant']
    assert json.loads(install['stdout'])['pid'] != json.loads(invoke['stdout'])['pid']
    assert (tmp_path/'community/capabilities/newsletter_probe.idcap').is_file()
    assert next(r for r in reader if r['action']=='reject_invalid_input')['passed']


def test_missing_artifact_records_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(sim, 'ACTORS', [('poet','identity_creator','poetry editor')])
    # Existing corrupt bytes let the summary hash evidence while installation fails.
    artifact=tmp_path/'corrupt.idcap'
    artifact.write_bytes(b'not a package')
    summary=sim.run(tmp_path/'failed',artifact)
    assert any(f['action']=='install_engineer_capability' for f in summary['failures'])
    assert summary['passed'] < summary['steps']
