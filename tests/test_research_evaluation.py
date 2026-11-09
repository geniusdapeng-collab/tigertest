"""Frozen denominators, source-bound independent review and temporal attribution."""
from copy import deepcopy
from pathlib import Path
import hashlib
import json

import pytest

from trading_system.agent_evidence import json_bytes
from trading_system.research_evaluation import (
    EvaluationError, fingerprint, freeze_cohort, reliability_report, aggregate_records,
    quality_report, validate_attribution,
)


def write(root, name, value):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(json_bytes(value))
    raw = path.read_bytes()
    return {'path': name, 'sha256': hashlib.sha256(raw).hexdigest(), 'bytes': len(raw)}


def corpus(tmp_path):
    text = 'TEST_FIXTURE: Revenue USD 10 million. A one-time item explains USD 4 million.'
    (tmp_path / 'source.txt').write_text(text)
    labels = []
    for name, quote in [('revenue', 'USD 10 million'), ('counterevidence', 'one-time item explains USD 4 million')]:
        start = text.index(quote)
        labels.append({'id': name, 'quote': quote, 'start': start, 'end': start + len(quote),
                       'severity': 'critical', 'expected': {'value': quote}})
    manifest = {'schema': 'tiger.repair-quality-gold/v1',
        'cases': [{'id': 'fixture', 'split': 'heldout', 'source': 'source.txt',
                   'source_sha256': hashlib.sha256(text.encode()).hexdigest(), 'labels': labels,
                   'provenance': {'kind': 'synthetic_fixture'}}],
        'rubric': {'critical_error_tolerance': 0, 'required_critical_fact_recall': 1.0,
                   'required_counterevidence_recall': 1.0}}
    manifest['sha256'] = fingerprint(manifest)
    write(tmp_path, 'manifest.json', manifest)
    artifact = {'annotation': {'facts': [row['quote'] for row in labels]}}
    ref = write(tmp_path, 'captures/annotation.json', artifact)
    capture = {'schema': 'tiger.repair-quality-capture/v1', 'gold_sha256': manifest['sha256'],
        'records': [{'case_id': 'fixture', 'source_sha256': manifest['cases'][0]['source_sha256'],
            'mode': 'fixture_replay', 'artifact_ref': ref, 'generator': 'fixture-author',
            'review': {'reviewer': 'independent-fixture-reviewer', 'independent_of_generation': True,
                'reviewed_at': '2026-11-08T00:00:00Z', 'artifact_sha256': ref['sha256'],
                'labels': [{'label_id': row['id'], 'verdict': 'pass',
                    'evidence_paths': ['/annotation/facts/' + str(i)], 'reason': 'TEST_FIXTURE verified field and qualification'}
                    for i, row in enumerate(labels)]}}]}
    return manifest, capture


def test_missing_and_unknown_quality_rows_retain_full_frozen_denominator(tmp_path):
    manifest, capture = corpus(tmp_path)
    empty = quality_report(tmp_path, {**capture, 'records': []}, tmp_path)
    assert empty['critical_recall'] == {'numerator': 0, 'denominator': 2, 'value': 0.0}
    assert empty['review_gate'] == 'incomplete' and empty['semantic_quality_certified'] is False
    capture['records'][0]['review']['labels'][1]['verdict'] = 'unknown'
    result = quality_report(tmp_path, capture, tmp_path)
    assert result['critical_recall']['denominator'] == 2 and result['critical_recall']['numerator'] == 1
    assert result['review_gate'] == 'incomplete'


def test_review_is_source_and_artifact_bound_and_cannot_self_award(tmp_path):
    _, capture = corpus(tmp_path)
    result = quality_report(tmp_path, capture, tmp_path)
    assert result['review_gate'] == 'pass' and result['evidence_scope'] == 'fixture_only'
    assert result['semantic_quality_certified'] is False
    for mutate in [lambda c: c['records'][0].update(source_sha256='0' * 64),
                   lambda c: c['records'][0]['review'].update(artifact_sha256='0' * 64),
                   lambda c: c['records'][0]['review'].update(reviewer='fixture-author'),
                   lambda c: c['records'][0]['review']['labels'][0].update(evidence_paths=['/not-present']),
                   lambda c: c['records'][0]['review']['labels'].append(deepcopy(c['records'][0]['review']['labels'][0]))]:
        altered = deepcopy(capture); mutate(altered)
        with pytest.raises(EvaluationError):
            quality_report(tmp_path, altered, tmp_path)
    (tmp_path / 'captures/annotation.json').write_text('{}')
    with pytest.raises(EvaluationError):
        quality_report(tmp_path, capture, tmp_path)


def test_critical_wrong_fact_blocks_even_with_all_citations_present(tmp_path):
    _, capture = corpus(tmp_path)
    capture['records'][0]['review']['labels'][0]['verdict'] = 'wrong'
    result = quality_report(tmp_path, capture, tmp_path)
    assert result['review_gate'] == 'blocked' and result['critical_errors'] == 1
    assert result['strata']['heldout']['denominator'] == 2


def test_frozen_real_corpus_is_checked_without_modifying_labels(tmp_path):
    gold = Path(__file__).parent / 'fixtures/repair_quality'
    before = (gold / 'manifest.json').read_bytes()
    manifest = json.loads(before)
    capture = {'schema': 'tiger.repair-quality-capture/v1', 'gold_sha256': manifest['sha256'], 'records': []}
    result = quality_report(gold, capture, tmp_path)
    assert result['critical_recall']['denominator'] == 23
    assert result['cases'] == 7 and result['review_gate'] == 'incomplete'
    assert result['strata']['heldout']['denominator'] == 15
    assert (gold / 'manifest.json').read_bytes() == before


def test_freeze_preserves_invalid_failed_and_later_missing_admissions(tmp_path):
    state = tmp_path / 'state'; (state / 'runs').mkdir(parents=True)
    ids = ['run-' + value * 32 for value in 'ab']
    for identity in ids:
        write(state, 'runs/' + identity + '/admission.json', {'run_id': identity, 'TEST_FIXTURE': True})
    cohort = freeze_cohort(state, kind='fixture_replay', now='2026-11-08T00:00:00Z')
    assert [entry['run_id'] for entry in cohort['entries']] == ids
    (state / 'runs' / ids[1] / 'admission.json').unlink()
    result = reliability_report(state, cohort)
    assert result['overall']['business_complete_over_admitted_runs'] == {'numerator': 0, 'denominator': 2, 'value': 0.0}
    assert len(result['runs']) == 2 and all(row['verification_error'] for row in result['runs'])
    assert result['overall']['usage']['unknown_runs'] == 2


def row(identity, *, source='a', status='failed', complete=False, unknown=1, originals=10):
    return {'run_id': identity, 'profile': 'test-profile', 'source_sha256': source * 64,
            'policy_sha256': 'c' * 64, 'contract': 'test-contract', 'mode': 'fixture_replay',
            'as_of': '2026-11-08T00:00:00Z', 'status': status, 'full_chain_verified': complete,
            'elapsed_seconds': 12.0 if complete else None, 'originals': originals,
            'eligible_originals': 5, 'validated_originals': 7, 'first_attempt_valid_originals': 6,
            'recoverable_units': 2, 'recovered_units': 1,
            'coverage_present': 3, 'coverage_required': 4,
            'known_usage': {'prompt_tokens': 7, 'completion_tokens': 3, 'total_tokens': 10},
            'unknown_attempts': unknown, 'verification_error': None}


def test_metrics_keep_unknown_failed_runs_and_do_not_mix_versions():
    records = [row('a'), row('b', complete=True, status='completed', unknown=0), row('c', source='b')]
    result = aggregate_records(records)
    assert result['overall']['business_complete_over_admitted_runs']['denominator'] == 3
    assert result['overall']['usage']['known_total_tokens'] == 30
    assert result['overall']['usage']['complete_total_tokens'] is None
    assert result['overall']['usage']['unknown_attempts'] == 2
    assert result['overall']['elapsed_seconds']['samples'] == 1
    assert result['overall']['elapsed_seconds']['missing'] == 2
    assert len(result['strata']) == 2 and result['overall']['heterogeneous'] is True
    assert result['overall']['first_attempt_valid_over_admitted_originals']['denominator'] == 30
    assert result['overall']['recovered_over_failed_recoverable'] == {'numerator': 3, 'denominator': 6, 'value': .5}
    records[0]['originals'] = None
    result = aggregate_records(records)
    assert result['overall']['first_attempt_valid_over_admitted_originals']['unknown_denominator_runs'] == 1


def test_empty_cohort_has_no_invented_zero_success_or_latency():
    result = aggregate_records([])
    assert result['overall']['business_complete_over_admitted_runs']['value'] is None
    assert result['overall']['elapsed_seconds']['p95'] is None


def attribution(tmp_path):
    original = write(tmp_path, 'original.json', {'fact': 'TEST_FIXTURE then-known source'})
    later = write(tmp_path, 'later.json', {'observation': 'TEST_FIXTURE later market move'})
    binding = {'run_id': 'run-' + 'a' * 32, 'source_sha256': '1' * 64,
        'snapshot_sha256': '2' * 64, 'as_of': '2026-11-08T00:00:00Z', 'account_kind': 'paper',
        'account_version_sha256': '3' * 64,
        'ex_ante_evidence': [{**original, 'available_at': '2026-11-07T00:00:00Z'}]}
    record = {'schema': 'tiger.research-attribution/v1',
        **{key: value for key, value in binding.items() if key != 'ex_ante_evidence'},
        'reviewer': 'independent-fixture-reviewer', 'category': 'market_outcome',
        'causality': 'hypothesis', 'basis': 'ex_post', 'finding': 'TEST_FIXTURE later move, not proof of model error',
        'evidence': [{**later, 'available_at': '2026-11-09T00:00:00Z'}], 'rule_ref': None}
    return binding, record


def test_attribution_keeps_later_market_outcome_out_of_original_decision(tmp_path):
    binding, record = attribution(tmp_path)
    checked = validate_attribution(tmp_path, record, binding)
    assert checked['basis'] == 'ex_post' and checked['account_kind'] == 'paper'
    for updates in [{'basis': 'ex_ante'}, {'account_kind': 'real'}, {'source_sha256': '0' * 64},
                    {'category': 'risk_policy', 'rule_ref': None}, {'causality': 'proven_profit'}]:
        with pytest.raises(EvaluationError):
            validate_attribution(tmp_path, {**record, **updates}, binding)


def test_attribution_exante_cannot_add_a_late_new_source_with_backdated_label(tmp_path):
    binding, record = attribution(tmp_path)
    record.update(category='data_omission', basis='ex_ante')
    record['evidence'][0]['available_at'] = '2026-11-07T00:00:00Z'
    with pytest.raises(EvaluationError):
        validate_attribution(tmp_path, record, binding)
    record['evidence'] = deepcopy(binding['ex_ante_evidence'])
    assert validate_attribution(tmp_path, record, binding)['category'] == 'data_omission'
