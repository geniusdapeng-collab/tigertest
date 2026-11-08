"""Corpus-integrity checks only; passing these is not a semantic benchmark."""
import hashlib
import json
from pathlib import Path

from trading_system.intelligence.contracts import digest

ROOT = Path(__file__).parent / 'fixtures' / 'repair_quality'
FROZEN_SHA256 = 'a27ec21cf36d2c3dc83d058a13b4e476aaf5fc1210c5bcee77578b7077dc9872'


def test_frozen_independent_labels_are_source_bound_and_split():
    manifest=json.loads((ROOT/'manifest.json').read_text())
    assert manifest['sha256'] == FROZEN_SHA256
    assert digest({k:v for k,v in manifest.items() if k != 'sha256'}) == FROZEN_SHA256
    sources=set(); counts={'development':0,'heldout':0}
    for case in manifest['cases']:
        assert case['source'] not in sources
        sources.add(case['source']);counts[case['split']] += 1
        assert case['source'].startswith(case['split'] + '/')
        raw=(ROOT/case['source']).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == case['source_sha256']
        text=raw.decode()
        for label in case['labels']:
            assert text[label['start']:label['end']] == label['quote']
            assert label['severity'] == 'critical'
    assert counts == {'development':3, 'heldout':4}
    assert manifest['rubric']['critical_error_tolerance'] == 0
    assert manifest['rubric']['missing_or_unknown'] == 'retain denominator and mark incomplete'
