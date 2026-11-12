"""Offline quality review, immutable cohorts and evidence-bound retrospectives.

This reader never calls a model, changes an account, or loads gold labels into
research execution. Human/independent-agent judgments remain explicitly named
judgments; content hashes provide local integrity, not reviewer authentication.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re

from .agent_evidence import (EvidenceInvalid, checked_root, read_bytes, read_json,
                             strict_json, atomic_json)

GOLD_SCHEMA = 'tiger.repair-quality-gold/v1'
CAPTURE_SCHEMA = 'tiger.repair-quality-capture/v1'
COHORT_SCHEMA = 'tiger.research-cohort/v1'
RUN_PATTERN = re.compile(r'run-[0-9a-f]{32}\Z')
HASH_PATTERN = re.compile(r'[0-9a-f]{64}\Z')


class EvaluationError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def fingerprint(value):
    try:
        raw = json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
                         allow_nan=False).encode('utf-8')
        return hashlib.sha256(raw).hexdigest()
    except (TypeError, ValueError, OverflowError, RecursionError):
        raise EvaluationError('EVALUATION_JSON_INVALID') from None


def _require(condition, code='EVALUATION_CONTRACT_INVALID'):
    if not condition:
        raise EvaluationError(code)


def _time(value):
    try:
        _require(type(value) is str)
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
        _require(result.utcoffset() is not None)
        return result.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        raise EvaluationError('EVALUATION_TIME_INVALID') from None


def _hash(value):
    return type(value) is str and HASH_PATTERN.fullmatch(value) is not None


def _ref_bytes(root, ref):
    _require(type(ref) is dict and set(ref) == {'path', 'sha256', 'bytes'}
             and _hash(ref['sha256']) and type(ref['bytes']) is int and ref['bytes'] >= 0)
    try:
        data = read_bytes(Path(root), ref['path'])
    except (EvidenceInvalid, OSError):
        raise EvaluationError('EVALUATION_ARTIFACT_UNREADABLE') from None
    _require(len(data) == ref['bytes'] and hashlib.sha256(data).hexdigest() == ref['sha256'],
             'EVALUATION_ARTIFACT_CHANGED')
    return data


def _pointer(value, path):
    _require(type(path) is str and path.startswith('/') and len(path) <= 4096,
             'EVALUATION_POINTER_INVALID')
    try:
        for component in path[1:].split('/'):
            _require(re.search(r'~(?![01])', component) is None, 'EVALUATION_POINTER_INVALID')
            component = component.replace('~1', '/').replace('~0', '~')
            if type(value) is list:
                _require(re.fullmatch(r'0|[1-9][0-9]*', component) is not None)
                value = value[int(component)]
            elif type(value) is dict:
                value = value[component]
            else:
                raise EvaluationError('EVALUATION_POINTER_INVALID')
        _require(value is not None and value != '' and value != [] and value != {},
                 'EVALUATION_POINTER_EMPTY')
        return value
    except (KeyError, IndexError, ValueError, TypeError):
        raise EvaluationError('EVALUATION_POINTER_INVALID') from None


def _ratio(numerator, denominator):
    return {'numerator': numerator, 'denominator': denominator,
            'value': numerator / denominator if denominator else None}


def _gold(root):
    try:
        manifest = read_json(Path(root), 'manifest.json')
        _require(type(manifest) is dict and manifest.get('schema') == GOLD_SCHEMA
                 and manifest.get('sha256') == fingerprint({k: v for k, v in manifest.items() if k != 'sha256'}),
                 'GOLD_MANIFEST_CHANGED')
        cases, sources = {}, {}
        _require(type(manifest.get('cases')) is list and bool(manifest['cases']))
        for case in manifest['cases']:
            _require(type(case) is dict and type(case.get('id')) is str and case['id'] not in cases
                     and case.get('split') in {'development', 'heldout'}
                     and type(case.get('labels')) is list and bool(case['labels']))
            data = read_bytes(Path(root), case['source'])
            _require(hashlib.sha256(data).hexdigest() == case['source_sha256'], 'GOLD_SOURCE_CHANGED')
            body, labels = data.decode('utf-8'), set()
            for label in case['labels']:
                _require(type(label) is dict and type(label.get('id')) is str and label['id'] not in labels
                         and label.get('severity') == 'critical' and type(label.get('expected')) is dict
                         and type(label.get('start')) is int and type(label.get('end')) is int
                         and 0 <= label['start'] < label['end'] <= len(body)
                         and body[label['start']:label['end']] == label['quote'], 'GOLD_LABEL_UNBOUND')
                labels.add(label['id'])
            cases[case['id']], sources[case['id']] = case, body
        _require(manifest['rubric']['critical_error_tolerance'] == 0
                 and manifest['rubric']['required_critical_fact_recall'] == 1.0
                 and manifest['rubric']['required_counterevidence_recall'] == 1.0, 'GOLD_RUBRIC_CHANGED')
        return manifest, cases, sources
    except (EvidenceInvalid, OSError, KeyError, UnicodeError):
        raise EvaluationError('GOLD_INPUT_INVALID') from None


def quality_report(gold_root, capture, artifact_root):
    """Score independent, hash-bound adjudications; missing labels stay missing."""
    manifest, cases, _sources = _gold(gold_root)
    _require(type(capture) is dict and set(capture) == {'schema', 'gold_sha256', 'records'}
             and capture['schema'] == CAPTURE_SCHEMA and capture['gold_sha256'] == manifest['sha256']
             and type(capture['records']) is list, 'QUALITY_CAPTURE_INVALID')
    captured, modes = {}, set()
    for record in capture['records']:
        _require(type(record) is dict and set(record) == {
            'case_id', 'source_sha256', 'mode', 'artifact_ref', 'generator', 'review'}
            and record['case_id'] in cases and record['case_id'] not in captured
            and record['source_sha256'] == cases[record['case_id']]['source_sha256']
            and record['mode'] in {'fixture_replay', 'frozen_native'}, 'QUALITY_RECORD_INVALID')
        try:
            artifact = strict_json(_ref_bytes(artifact_root, record['artifact_ref']))
        except (ValueError, UnicodeError):
            raise EvaluationError('QUALITY_ARTIFACT_INVALID') from None
        review = record['review']
        _require(type(review) is dict and set(review) == {
            'reviewer', 'independent_of_generation', 'reviewed_at', 'artifact_sha256', 'labels'}
            and type(review['reviewer']) is str and bool(review['reviewer'].strip())
            and type(record['generator']) is str and bool(record['generator'].strip())
            and review['reviewer'] != record['generator'] and review['independent_of_generation'] is True
            and review['artifact_sha256'] == record['artifact_ref']['sha256']
            and type(review['labels']) is list, 'QUALITY_REVIEW_UNBOUND')
        _time(review['reviewed_at'])
        labels, expected = {}, {row['id'] for row in cases[record['case_id']]['labels']}
        for judgment in review['labels']:
            _require(type(judgment) is dict and set(judgment) == {
                'label_id', 'verdict', 'evidence_paths', 'reason'}
                and judgment['label_id'] in expected and judgment['label_id'] not in labels
                and judgment['verdict'] in {'pass', 'wrong', 'missing', 'unknown'}
                and type(judgment['evidence_paths']) is list
                and len(judgment['evidence_paths']) == len(set(judgment['evidence_paths']))
                and type(judgment['reason']) is str and bool(judgment['reason'].strip()), 'QUALITY_LABEL_INVALID')
            _require(judgment['verdict'] != 'pass' or bool(judgment['evidence_paths']), 'QUALITY_PASS_WITHOUT_EVIDENCE')
            for pointer in judgment['evidence_paths']:
                _pointer(artifact, pointer)
            labels[judgment['label_id']] = judgment
        modes.add(record['mode'])
        captured[record['case_id']] = (record, labels)
    rows = []
    for case in cases.values():
        entry = captured.get(case['id'])
        judgments = {} if entry is None else entry[1]
        for label in case['labels']:
            judgment = judgments.get(label['id'])
            rows.append({'case_id': case['id'], 'split': case['split'], 'label_id': label['id'],
                'source_kind': case['provenance']['kind'], 'source_sha256': case['source_sha256'],
                'verdict': 'not_run' if judgment is None else judgment['verdict'],
                'artifact_sha256': None if entry is None else entry[0]['artifact_ref']['sha256'],
                'reviewer': None if entry is None else entry[0]['review']['reviewer'],
                'evidence_paths': [] if judgment is None else judgment['evidence_paths']})
    passed = sum(row['verdict'] == 'pass' for row in rows)
    wrong = sum(row['verdict'] == 'wrong' for row in rows)
    return {'schema': 'tiger.repair-quality-report/v1', 'gold_sha256': manifest['sha256'],
        'capture_sha256': fingerprint(capture), 'cases': len(cases), 'reviewed_cases': len(captured),
        'critical_recall': _ratio(passed, len(rows)), 'critical_errors': wrong,
        'review_gate': 'blocked' if wrong else 'pass' if passed == len(rows) else 'incomplete',
        'evidence_scope': 'not_measured' if not modes else 'fixture_only' if modes == {'fixture_replay'} else 'native_and_reviewed_inputs',
        'semantic_quality_certified': False,
        'strata': {split: _ratio(sum(row['verdict'] == 'pass' for row in rows if row['split'] == split),
                                sum(row['split'] == split for row in rows)) for split in ('development', 'heldout')},
        'labels': rows,
        'limitations': ['Review verdicts are independent reviewer assertions; hashes are not identity signatures.',
            'A verified pointer proves the cited output exists, not that its financial interpretation is true.',
            'Fixtures and native captures are distinguished; passing this corpus is not a production SLA.',
            'Wrong critical facts block; missing, unknown and unrun labels remain in the denominator.']}


def freeze_cohort(state, *, kind, now=None):
    """Freeze every admitted run visible in this state; never select only winners."""
    _require(kind in {'fixture_replay', 'historical', 'prospective'}, 'COHORT_KIND_INVALID')
    state = checked_root(Path(state))
    stamp = now or datetime.now(timezone.utc).isoformat()
    _time(stamp)
    try:
        paths = sorted((state / 'runs').iterdir(), key=lambda item: item.name)
    except OSError:
        raise EvaluationError('COHORT_STATE_UNREADABLE') from None
    entries = []
    for run in paths:
        if not run.name.startswith('run-'):
            continue
        _require(RUN_PATTERN.fullmatch(run.name) is not None, 'COHORT_RUN_ID_INVALID')
        try:
            data = read_bytes(run, 'admission.json')
            checksum, error = hashlib.sha256(data).hexdigest(), None
        except (EvidenceInvalid, OSError):
            checksum, error = None, 'ADMISSION_UNREADABLE'
        entries.append({'run_id': run.name, 'admission_sha256': checksum, 'freeze_error': error})
    value = {'schema': COHORT_SCHEMA, 'kind': kind, 'frozen_at': stamp,
             'selection': 'all_run_directories_in_state', 'entries': entries}
    value['sha256'] = fingerprint(value)
    return value


def _empty_record(entry, mode):
    return {'run_id': entry['run_id'], 'profile': 'unknown', 'source_sha256': None, 'policy_sha256': None,
        'contract': 'unknown', 'mode': mode, 'as_of': None, 'status': 'unknown', 'full_chain_verified': False,
        'elapsed_seconds': None, 'originals': None, 'eligible_originals': None,
        'validated_originals': None, 'first_attempt_valid_originals': None,
        'recoverable_units': None, 'recovered_units': None, 'coverage_present': None, 'coverage_required': None,
        'known_usage': None, 'unknown_attempts': None, 'verification_error': None,
        'evidence': {}}


def _collect_record(state, entry, mode):
    from .agent_runtime import validate_admission, verify_run, INTELLIGENCE_PROFILES
    from .agent_runner import observe_run
    record = _empty_record(entry, mode)
    run = state / 'runs' / entry['run_id']
    try:
        _require(entry['admission_sha256'] is not None and entry['admission_sha256'] ==
                 hashlib.sha256(read_bytes(run, 'admission.json')).hexdigest(), 'COHORT_ADMISSION_CHANGED')
        admission, policy, source = validate_admission(run, validate_working_calendar=False)
        record.update(profile=policy['profile'], source_sha256=admission['source_sha256'],
            policy_sha256=admission['policy_sha256'], as_of=admission['clock']['admitted_at_utc'],
            contract=policy.get('research_work', {}).get('contract', 'historical-five-stage'))
        observation = observe_run(state, entry['run_id'])
        record['status'] = observation['status']
        record['evidence']['admission_sha256'] = entry['admission_sha256']
        model = None
        if observation['commitment_present']:
            verified = verify_run(state, entry['run_id'], current_source=None)
            record['full_chain_verified'] = verified['status'] == 'completed'
            record['evidence']['receipt_sha256'] = verified['verification']['receipt_sha256']
            receipt = read_json(run, 'receipt.json')
            record['elapsed_seconds'] = (_time(receipt['sealed_at_utc']) - _time(record['as_of'])).total_seconds()
            model = verified['summary'].get('model')
            intel = verified['summary'].get('disclosures', {}).get('intelligence', {})
            gaps = intel.get('gaps')
            if type(gaps) is list:
                required = [row for row in gaps if row.get('required') is True]
                record['coverage_required'] = len(required)
                record['coverage_present'] = sum(row.get('status') == 'present' for row in required)
        elif policy['profile'] in INTELLIGENCE_PROFILES and not observation['active'] and (run / 'events.jsonl').exists():
            from .llm.intelligence_proof import verify_partial_intelligence_model
            model = verify_partial_intelligence_model(run, admission,
                scene_policy_path=run / 'source/governance/bundles/trading/model-policy.yml')
            record['evidence']['partial_model_sha256'] = fingerprint(model)
        if model is not None:
            record['known_usage'] = model.get('known_usage', model.get('usage'))
            record['unknown_attempts'] = model.get('unknown_attempts', 0 if record['full_chain_verified'] else None)
        if policy.get('research_work') is not None and (run / 'tasks').exists() and not observation['active']:
            from .tasks import WorkStore
            from .research_work import work_manifest
            store = WorkStore.open(run / 'tasks', work_manifest(admission))
            record['evidence']['work_ledger'] = store.verify()
            status, documents = store.status(), store.document_dispositions()
            record['originals'], record['eligible_originals'] = documents['admitted'], documents['eligible']
            # Processing completion and evidence eligibility are different
            # axes. A rejected/quarantined terminal document is not eventually
            # valid merely because every unit reached a terminal state.
            record['validated_originals'] = documents['eligible']
            # First attempt means every model unit for an original was validated
            # in epoch one and no second format attempt was dispatched.
            first = []
            for identity, disposition in documents['documents'].items():
                if disposition['state'] != 'eligible':
                    continue
                units = [row for row in status['units'] if identity in row['spec']['document_ids']]
                if units and all(row['state'] == 'validated' and row['epoch'] == 1
                    and all(attempt['ordinal'] == 1 for attempt in store.attempts_for_unit(row['unit_id'])) for row in units):
                    first.append(identity)
            record['first_attempt_valid_originals'] = len(first)
            attempts_by_unit = {}
            for attempt in store.attempts():
                attempts_by_unit.setdefault(attempt['unit_id'], []).append(attempt)
            recoverable = []
            for row in status['units']:
                attempts = attempts_by_unit.get(row['unit_id'], [])
                retried = row['epoch'] > 1 or any(attempt['ordinal'] > 1
                                                  for attempt in attempts)
                unresolved = (row['state'] == 'unknown'
                              or row['state'] == 'failed' and row['retryable'])
                restartable = row['state'] in {'ready', 'blocked'} and attempts and any(
                    attempt['state'] in {'not_sent', 'prepared', 'dispatched', 'observed'}
                    for attempt in attempts)
                if retried or unresolved or restartable:
                    recoverable.append(row)
            record['recoverable_units'] = len(recoverable)
            record['recovered_units'] = sum(row['state'] == 'validated' for row in recoverable)
    except (EvidenceInvalid, EvaluationError, OSError, KeyError, TypeError, ValueError) as error:
        record['full_chain_verified'] = False
        record['verification_error'] = getattr(error, 'code', 'EVALUATION_RUN_UNVERIFIED')
        # Never convert an unreadable accounting record into a free run.
        record['unknown_attempts'] = None
    return record


def _quantile(values, quantile):
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    low, high = math.floor(position), math.ceil(position)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def _stratum(row):
    return tuple(row.get(key) for key in ('profile', 'source_sha256', 'policy_sha256', 'contract', 'mode'))


def _aggregate(rows):
    def measured(numerator, denominator):
        known = [row for row in rows if row.get(numerator) is not None and row.get(denominator) is not None]
        value = _ratio(sum(row[numerator] for row in known), sum(row[denominator] for row in known))
        if len(known) != len(rows):
            value['unknown_denominator_runs'] = len(rows) - len(known)
        return value
    elapsed = [row['elapsed_seconds'] for row in rows if row.get('elapsed_seconds') is not None]
    usage = [row['known_usage'] for row in rows if row.get('known_usage') is not None]
    unknown_runs = sum(row.get('unknown_attempts') is None or row.get('known_usage') is None for row in rows)
    unknown_attempts = sum(row.get('unknown_attempts') or 0 for row in rows)
    known_tokens = sum(row['total_tokens'] for row in usage)
    return {'runs': len(rows), 'statuses': dict(Counter(row['status'] for row in rows)),
        'heterogeneous': len({_stratum(row) for row in rows}) > 1,
        'business_complete_over_admitted_runs': _ratio(sum(row['full_chain_verified'] for row in rows), len(rows)),
        'acquisition_eligible_over_observed': measured('eligible_originals', 'originals'),
        'required_evidence_cells_satisfied_over_required': measured('coverage_present', 'coverage_required'),
        'first_attempt_valid_over_admitted_originals': measured('first_attempt_valid_originals', 'originals'),
        'eventually_valid_over_admitted_originals': measured('validated_originals', 'originals'),
        'recovered_over_failed_recoverable': measured('recovered_units', 'recoverable_units'),
        'elapsed_seconds': {'samples': len(elapsed), 'missing': len(rows) - len(elapsed),
            'p50': _quantile(elapsed, .5), 'p95': _quantile(elapsed, .95)},
        'usage': {'known_total_tokens': known_tokens, 'unknown_attempts': unknown_attempts,
            'unknown_runs': unknown_runs, 'complete_total_tokens': known_tokens if not unknown_attempts and not unknown_runs else None}}


def aggregate_records(records):
    _require(type(records) is list and len({row['run_id'] for row in records}) == len(records), 'DUPLICATE_RUN_METRIC')
    strata = {}
    for key in sorted({_stratum(row) for row in records}, key=lambda value: str(value)):
        group = [row for row in records if _stratum(row) == key]
        strata[fingerprint(list(key))] = {'identity': dict(zip(
            ('profile', 'source_sha256', 'policy_sha256', 'contract', 'mode'), key)), **_aggregate(group)}
    return {'overall': _aggregate(records), 'strata': strata}


def reliability_report(state, cohort):
    _require(type(cohort) is dict and set(cohort) == {'schema', 'kind', 'frozen_at', 'selection', 'entries', 'sha256'}
        and cohort['schema'] == COHORT_SCHEMA and cohort['sha256'] == fingerprint(
            {key: value for key, value in cohort.items() if key != 'sha256'})
        and cohort['selection'] == 'all_run_directories_in_state'
        and cohort['kind'] in {'fixture_replay', 'historical', 'prospective'}
        and type(cohort['entries']) is list, 'COHORT_CHANGED')
    _time(cohort['frozen_at'])
    identities = set()
    for entry in cohort['entries']:
        _require(type(entry) is dict and set(entry) == {'run_id', 'admission_sha256', 'freeze_error'}
            and type(entry['run_id']) is str and RUN_PATTERN.fullmatch(entry['run_id']) is not None
            and entry['run_id'] not in identities
            and (entry['admission_sha256'] is None or _hash(entry['admission_sha256'])), 'COHORT_ENTRY_INVALID')
        identities.add(entry['run_id'])
    state = checked_root(Path(state))
    records = [_collect_record(state, entry, cohort['kind']) for entry in cohort['entries']]
    return {'schema': 'tiger.research-reliability/v1', 'cohort_sha256': cohort['sha256'],
        'cohort_frozen_at': cohort['frozen_at'], 'generated_at': datetime.now(timezone.utc).isoformat(),
        **aggregate_records(records), 'runs': records,
        'limitations': ['Cohort membership is immutable; later missing and invalid runs remain in denominators.',
            'Different profiles, source versions, policies and capture modes have separate strata.',
            'Work-unit validation is a mechanism result; only independent committed receipt proof counts as business completion.',
            'Unknown observations are not zero tokens; unmeasured document and time denominators remain explicitly unknown.',
            'This is descriptive reliability evidence, not a profitability or sustained-capacity claim.']}


def validate_attribution(root, record, binding):
    """Bind a retrospective to its original snapshot and separate later outcomes."""
    fields = {'schema', 'run_id', 'source_sha256', 'snapshot_sha256', 'as_of', 'account_kind',
        'account_version_sha256', 'reviewer', 'category', 'causality', 'basis', 'finding', 'evidence', 'rule_ref'}
    _require(type(record) is dict and set(record) == fields and record['schema'] == 'tiger.research-attribution/v1',
             'ATTRIBUTION_CONTRACT_INVALID')
    for key in ('run_id', 'source_sha256', 'snapshot_sha256', 'as_of', 'account_kind', 'account_version_sha256'):
        _require(record[key] == binding.get(key), 'ATTRIBUTION_ORIGINAL_BINDING_CHANGED')
    _require(RUN_PATTERN.fullmatch(record['run_id']) is not None
        and all(_hash(record[key]) for key in ('source_sha256', 'snapshot_sha256'))
        and (record['account_version_sha256'] is None or _hash(record['account_version_sha256']))
        and record['account_kind'] in {'paper', 'real', 'none'}
        and record['category'] in {'process_failure', 'data_omission', 'semantic_error', 'risk_policy', 'market_outcome'}
        and record['causality'] in {'supported', 'hypothesis', 'unknown'}
        and record['basis'] in {'ex_ante', 'ex_post'} and type(record['finding']) is str and bool(record['finding'].strip())
        and type(record['reviewer']) is str and bool(record['reviewer'].strip())
        and type(record['evidence']) is list and bool(record['evidence']), 'ATTRIBUTION_CONTRACT_INVALID')
    cutoff = _time(record['as_of'])
    _require(record['category'] != 'market_outcome' or record['basis'] == 'ex_post', 'ATTRIBUTION_FUTURE_LEAKAGE')
    _require(record['category'] != 'risk_policy' or record['rule_ref'] is not None, 'ATTRIBUTION_RULE_MISSING')
    for evidence in record['evidence']:
        _require(type(evidence) is dict and set(evidence) == {'path', 'sha256', 'bytes', 'available_at'})
        _ref_bytes(root, {key: value for key, value in evidence.items() if key != 'available_at'})
        if record['basis'] == 'ex_ante':
            _require(evidence in binding.get('ex_ante_evidence', []) and _time(evidence['available_at']) <= cutoff,
                     'ATTRIBUTION_FUTURE_LEAKAGE')
        else:
            _time(evidence['available_at'])
    if record['rule_ref'] is not None:
        _ref_bytes(root, record['rule_ref'])
    return strict_json(json.dumps(record, ensure_ascii=False, allow_nan=False))


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description='Read-only research quality, cohort and retrospective evaluator')
    sub = parser.add_subparsers(dest='operation', required=True)
    freeze = sub.add_parser('cohort'); freeze.add_argument('--state', required=True)
    freeze.add_argument('--kind', required=True, choices=('fixture_replay', 'historical', 'prospective'))
    freeze.add_argument('--out', required=True)
    reliability = sub.add_parser('reliability'); reliability.add_argument('--state', required=True)
    reliability.add_argument('--cohort', required=True); reliability.add_argument('--out', required=True)
    quality = sub.add_parser('quality'); quality.add_argument('--gold', required=True)
    quality.add_argument('--capture', required=True); quality.add_argument('--artifacts', required=True)
    quality.add_argument('--out', required=True)
    attribution = sub.add_parser('attribution'); attribution.add_argument('--record', required=True)
    attribution.add_argument('--binding', required=True); attribution.add_argument('--artifacts', required=True)
    attribution.add_argument('--out', required=True)
    args = parser.parse_args(argv)
    def load(path):
        path = Path(path).absolute()
        return read_json(path.parent, path.name)
    try:
        if args.operation == 'cohort':
            value = freeze_cohort(Path(args.state).absolute(), kind=args.kind)
        elif args.operation == 'reliability':
            value = reliability_report(Path(args.state).absolute(), load(args.cohort))
        elif args.operation == 'quality':
            value = quality_report(Path(args.gold).absolute(), load(args.capture), Path(args.artifacts).absolute())
        else:
            value = validate_attribution(Path(args.artifacts).absolute(), load(args.record), load(args.binding))
        path = Path(args.out).absolute()
        checked_root(path.parent, create=True)
        atomic_json(path.parent, path.name, value, exclusive=True)
        print(json.dumps({'ok': True, 'schema': value['schema'], 'artifact': str(path)}, ensure_ascii=False))
        return 0
    except (EvidenceInvalid, EvaluationError, OSError, ValueError, KeyError, TypeError) as error:
        print(json.dumps({'ok': False, 'code': getattr(error, 'code', 'EVALUATION_FAILED')}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
