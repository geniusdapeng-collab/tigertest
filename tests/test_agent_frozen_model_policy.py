"""The isolated worker must be able to admit the required-model profile."""
import json
import subprocess
import sys

from trading_system.agent_runtime import REQUIRED_PROFILE, SOURCE_ROOT, freeze_source, policy_manifest


SCENE_POLICY = 'governance/bundles/trading/model-policy.yml'


def test_frozen_worker_loads_required_model_policy_without_checkout(tmp_path):
    frozen = freeze_source(tmp_path)
    files = {ref['path'] for ref in frozen['files']}
    assert SCENE_POLICY in files
    assert (tmp_path / 'source' / SCENE_POLICY).read_bytes() == (SOURCE_ROOT / SCENE_POLICY).read_bytes()

    result = subprocess.run([
        sys.executable, '-I', '-B', '-c',
        'import json,sys; sys.path.insert(0,sys.argv[1]); '
        'from trading_system.agent_runtime import policy_manifest; '
        'print(json.dumps(policy_manifest(sys.argv[2])["model_contract"]["thinking_policy"]))',
        str(tmp_path / 'source'), REQUIRED_PROFILE,
    ], cwd=tmp_path, capture_output=True, text=True, timeout=15, check=True)
    assert json.loads(result.stdout) == policy_manifest(REQUIRED_PROFILE)['model_contract']['thinking_policy']
