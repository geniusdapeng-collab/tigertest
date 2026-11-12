"""A resumed stage reuses its own work without scheduling later stages."""
import pytest
from test_task_work_units import create, unit, accept, valid
from trading_system.tasks import Coordinator, WorkUnitError


def test_completed_scope_remains_reusable_when_later_analysis_failed(tmp_path):
    store = create(tmp_path, [unit('clean'), unit('analysis', dependencies=('clean',))])
    accept(store, store.claim('clean', 'original'))
    store.fail(store.claim('analysis', 'original'), 'TEST_FIXTURE_ANALYSIS_FAILED', retryable=False)
    coordinator = Coordinator(store, unit_ids=['clean'])
    try:
        result = coordinator.run({'extract': lambda _: pytest.fail('must reuse original result')},
                                 validators={'extract': valid}, timeout=1)
        assert result['scope_state'] == 'completed' and result['graph_state'] == 'failed'
        assert store.unit('analysis')['error_code'] == 'TEST_FIXTURE_ANALYSIS_FAILED'
    finally:
        coordinator.close()


def test_stage_scope_does_not_claim_unrelated_ready_work(tmp_path):
    store = create(tmp_path, [unit('clean'), unit('unrelated')])
    called = []
    coordinator = Coordinator(store, max_workers=4, unit_ids=['clean'])
    try:
        result = coordinator.run({'extract': lambda ctx: called.append(ctx.lease.unit_id) or {'ok': True}},
                                 validators={'extract': valid}, timeout=2)
        assert called == ['clean'] and result['scope_state'] == 'completed'
        assert store.unit('unrelated')['state'] == 'ready'
    finally:
        coordinator.close()


@pytest.mark.parametrize('scope', [[], ['missing'], ['a', 'a']])
def test_empty_missing_and_duplicate_scopes_fail_before_scheduling(tmp_path, scope):
    store = create(tmp_path)
    with pytest.raises(WorkUnitError):
        Coordinator(store, unit_ids=scope)
