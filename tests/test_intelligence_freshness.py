"""Fixed-cutoff freshness tests; source publication is never rewritten."""
from copy import deepcopy

import pytest

from trading_system.intelligence.contracts import IntelligenceError, source_time
from trading_system.intelligence.freshness import material_freshness


AS_OF = '2026-11-06T12:00:00+00:00'


def document(when):
    return {'times': {'published': source_time(when),
        'first_seen': source_time('2026-11-06T11:59:00+00:00'),
        'retrieved': source_time('2026-11-06T11:59:00+00:00'),
        'updated': source_time('2026-11-06T11:59:00+00:00')}}


@pytest.mark.parametrize('dimension,horizon,qualified', [
    ('issuer_ir', 'current', True), ('issuer_ir', 'week', False),
    ('news_events', 'current', False), ('news_events', 'month', True),
    ('issuer_ir', 'baseline', True),
])
def test_source_types_and_explicit_question_windows_differ(dimension, horizon, qualified):
    original = document('2026-10-28T10:00:00+00:00')
    before = deepcopy(original)
    checked = material_freshness(original, dimension, horizon, AS_OF)
    assert checked['qualified'] is qualified and original == before


def test_monthly_statistic_can_fill_its_typed_window_but_not_weekly_news():
    original = document('2026-10-16T13:15:00+00:00')
    assert material_freshness(original, 'industry_data', 'current', AS_OF)['qualified']
    assert not material_freshness(original, 'news_events', 'week', AS_OF)['qualified']
    assert not material_freshness(document('2023-10-16T13:15:00+00:00'), 'industry_data', 'baseline', AS_OF)['qualified']


def test_current_retrieval_and_modified_date_cannot_refresh_old_opinion():
    checked = material_freshness(document('2026-09-01T00:00:00+00:00'), 'person_statement', 'current', AS_OF)
    assert not checked['qualified'] and checked['reason'] == 'MATERIAL_OUTSIDE_REQUESTED_TIME_WINDOW'


@pytest.mark.parametrize('when,expected', [
    ('2026-11-06', 'MATERIAL_PUBLICATION_CUTOFF_UNVERIFIED'),
    ('2026-11-07T10:00:00+00:00', 'MATERIAL_PUBLICATION_AFTER_CUTOFF'),
    ('2026-11-15', 'MATERIAL_PUBLICATION_AFTER_CUTOFF'),
    ('2026-10-30', 'MATERIAL_TIME_WINDOW_BOUNDARY_UNVERIFIED'),
])
def test_unknown_intraday_or_window_boundary_does_not_fill_current_coverage(when, expected):
    checked = material_freshness(document(when), 'news_events', 'week', AS_OF)
    assert not checked['qualified'] and checked['reason'] == expected


def test_historical_naive_episode_uses_only_conservative_calendar_bounds():
    original = document('2026-09-01T00:00:00')
    before = deepcopy(original)
    checked = material_freshness(original, 'person_statement', 'baseline', AS_OF)
    assert checked['qualified'] and checked['publication_precision'] == 'unknown'
    assert checked['date_basis'] == 'source_local_date_only_publication_instant_unknown'
    assert checked['comparison_interval']['bounds_are_not_source_timestamps']
    assert original == before and original['times']['published']['value'] is None
    assert not material_freshness(original, 'person_statement', 'current', AS_OF)['qualified']


@pytest.mark.parametrize('when', [None, 'not a time', '2026-13-32T12:00:00', '2026-11-06T12:00:00'])
def test_missing_invalid_or_same_day_naive_publication_stays_unknown(when):
    checked = material_freshness(document(when), 'news_events', 'week', AS_OF)
    assert not checked['qualified']


def test_same_day_publication_with_real_timezone_is_precise_and_cutoff_bound():
    original = document('2026-11-06T10:00:00+00:00')
    assert material_freshness(original, 'news_events', 'week', AS_OF)['qualified']
    assert not material_freshness(original, 'news_events', 'week', '2026-11-06T09:59:00+00:00')['qualified']
    assert material_freshness(original, 'news_events', 'week', AS_OF) == material_freshness(original, 'news_events', 'week', AS_OF)


@pytest.mark.parametrize('dimension,horizon', [('invented', 'current'), ('news_events', 'forever')])
def test_unregistered_time_scope_is_not_silently_accepted(dimension, horizon):
    with pytest.raises(IntelligenceError):
        material_freshness(document('2026-11-01T00:00:00+00:00'), dimension, horizon, AS_OF)


@pytest.mark.parametrize('form', ['10-K', '10-Q/A', '20-F'])
def test_reviewed_current_financial_statement_is_background_not_new_catalyst(form):
    original = document('2026-08-01T10:00:00+00:00')
    original['source_metadata'] = {'form': form}
    original['authority'] = {'scope': 'sec_filing', 'proof_type': 'sec_submission_acceptance'}
    checked = material_freshness(original, 'issuer_disclosure', 'current', AS_OF)
    assert checked['qualified'] and checked['time_use'] == 'financial_statement_baseline'
    assert checked['window_days'] == 760
    assert not material_freshness(original, 'issuer_disclosure', 'week', AS_OF)['qualified']
    original['authority']['scope'] = 'discovery'
    assert not material_freshness(original, 'issuer_disclosure', 'current', AS_OF)['qualified']
