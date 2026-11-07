"""Strict, ordered daily execution and eagerly persisted evidence boundaries.

The runner owns admission, storage, process supervision and account publication.
This module cannot publish an account or certify its own persisted evidence.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path
from threading import get_ident
from time import perf_counter
from typing import Any, Callable, Mapping, Protocol

from .model_diagnostics import MODEL_IO_METHODS, model_failure_diagnostic


class EvidenceWriteError(RuntimeError):
    """Evidence could not be durably recorded; never a provider fallback."""


class ExecutionContractError(RuntimeError):
    """An execution attempted to violate its fixed stage/IO contract."""


class StrictDataUnavailable(RuntimeError):
    code = "DATA_UNAVAILABLE"


class StrictObservationError(StrictDataUnavailable):
    """Provider observation dates violate the frozen market-date boundary."""


class StrictDataStale(RuntimeError):
    code = "DATA_STALE"


class StrictSourceSynthetic(ExecutionContractError):
    code = "SOURCE_SYNTHETIC"


class RequiredModelFailed(ExecutionContractError):
    code = "MODEL_REQUIRED_FAILED"


REQUIRED_PROFILE = "us-public-model-required-paper-v1"
REQUIRED_POLICY_ID = "tiger.public-model-required-paper-policy.v1"
INTELLIGENCE_PROFILE = "us-public-intelligence-paper-v2"
INTELLIGENCE_POLICY_ID = "tiger.public-intelligence-paper-policy.v2"
MODEL_REQUIRED_POLICIES = frozenset({REQUIRED_POLICY_ID, INTELLIGENCE_POLICY_ID})
INTELLIGENCE_SEARCH_SOURCES = (
    "deepseek_search", "brave_news", "tavily_search", "exa_search",
    "sec_submissions", "issuer_ir", "fed", "fred", "bls", "cboe", "fda", "ecb",
)
REQUIRED_MODEL_STAGES = ("clean.llm_semantic", "tech.sentiment", "tech.risk",
                         "sector.narrative", "decision.debate")
DAILY_MACRO_OBSERVATION_CONTRACT = "source-midnight-macro-label/v1"


PUBLIC_PROVIDER_NAMES = ("yahoo", "stooq", "official", "tencent", "sina", "eastmoney")
PUBLIC_SEARCH_SOURCE_NAMES = (
    "google_news", "reddit", "edgar", "patentsview", "federal_register",
    "eastmoney_news", "sina_flash",
)


@dataclass(frozen=True)
class ExecutionPolicy:
    policy_id: str
    model_mode: str = "disabled"
    provider_names: tuple[str, ...] = PUBLIC_PROVIDER_NAMES
    search_source_names: tuple[str, ...] = PUBLIC_SEARCH_SOURCE_NAMES
    market_timezone: str = "America/New_York"

    def __post_init__(self):
        required = self.policy_id in MODEL_REQUIRED_POLICIES
        if not self.policy_id or self.model_mode != ("enabled" if required else "disabled"):
            raise ExecutionContractError("Strict public paper requires a disabled model policy")
        if self.market_timezone != "America/New_York":
            raise ExecutionContractError("Strict public paper has a fixed market timezone")
        for values, allowed, label in (
            (self.provider_names, PUBLIC_PROVIDER_NAMES, "provider"),
            (self.search_source_names, INTELLIGENCE_SEARCH_SOURCES
             if self.policy_id == INTELLIGENCE_POLICY_ID else
             ("deepseek_search",) if required else PUBLIC_SEARCH_SOURCE_NAMES, "search source"),
        ):
            if (not values or len(set(values)) != len(values)
                    or any(name not in allowed for name in values)):
                raise ExecutionContractError(f"Invalid public {label} allowlist")


@dataclass(frozen=True)
class RuntimePaths:
    work_dir: Path
    reports_dir: Path
    journal_path: Path
    sim_path: Path
    calibration_path: Path
    options_history_dir: Path
    governance_path: Path
    proposals_dir: Path
    calendar_override_path: Path

    def __post_init__(self):
        for name in self.__dataclass_fields__:
            object.__setattr__(self, name, Path(getattr(self, name)).resolve())
        for name in self.__dataclass_fields__:
            if name != "work_dir":
                value = getattr(self, name)
                if value == self.work_dir or not value.is_relative_to(self.work_dir):
                    raise ExecutionContractError(f"Runtime path outside unique work directory: {name}")

    def to_dict(self) -> dict[str, str]:
        return {name: str(getattr(self, name)) for name in self.__dataclass_fields__}


@dataclass(frozen=True)
class DailyStageSpec:
    name: str
    driver: str
    owner: str
    allow_passthrough: bool = False


def daily_stage_specs(model_required: bool = False, *,
                      authoritative_paper: bool = False,
                      intelligence: bool = False) -> tuple[DailyStageSpec, ...]:
    """The sole strict daily order: 3 setup + 20 registered pipeline + 16 tail."""
    from . import DEFERRED_STEPS, pipeline_steps
    setup = (
        DailyStageSpec("daily.preflight", "rules", "main"),
        DailyStageSpec("state.reset", "rules", "state"),
        DailyStageSpec("pipeline.prepare", "rules", "pipeline"),
    )
    pipeline = tuple(DailyStageSpec(s.name, s.driver, s.owner, s.driver == "llm" and not model_required)
                     for s in pipeline_steps(intelligence) if s.name not in DEFERRED_STEPS)
    paper = (("paper.step", "simulator"), ("paper.persist", "simulator"))
    journal = (("journal.log", "journal"), ("journal.settle", "journal"),
               ("journal.stats", "journal"))
    tail = tuple(DailyStageSpec(name, "rules", owner) for name, owner in (
        ("accounting.validate", "main"), ("report.initial", "report"),
        *(paper + journal if authoritative_paper else journal + paper),
        ("governance.initialize", "governance_bridge"),
        ("review.daily", "review.chief"), ("governance.emit", "governance_bridge"),
        ("governance.verify", "governance_bridge"), ("report.final_json", "report"),
        ("report.plan", "triggers"), ("report.markdown", "report"),
        ("report.html", "report_html"), ("daily.result", "main"),
    ))
    return setup + pipeline + tail


def daily_stage_contract(model_required: bool = False, *, authoritative_paper: bool = False,
                         intelligence: bool = False) -> list[dict]:
    return [asdict(spec) for spec in daily_stage_specs(
        model_required, authoritative_paper=authoritative_paper, intelligence=intelligence)]


class EvidenceStore(Protocol):
    def snapshot(self, *, stage: str, role: str, value: object) -> Mapping[str, object]: ...
    def append_event(self, event: Mapping[str, object]) -> Mapping[str, object]: ...


class _LegacyScope:
    def output(self, value: object) -> None:
        return None


@contextmanager
def optional_step(execution, name: str, inputs: object):
    if execution is None:
        yield _LegacyScope()
    else:
        with execution.step(name, inputs) as scope:
            yield scope


@dataclass
class _IOHandle:
    scope: "StageScope"
    index: int
    method: str
    input_ref: dict
    completed: bool = False


class StageScope:
    def __init__(self, execution: "ExecutionContext", spec: DailyStageSpec,
                 input_ref: dict, index: int):
        self.execution = execution
        self.spec = spec
        self.input_ref = input_ref
        self.index = index
        self.output_ref: dict | None = None
        self.reason = ""
        self.status = "running"
        self.started = perf_counter()
        self.elapsed_ms = 0.0
        self._handles: list[_IOHandle] = []
        self.io_calls: list[dict] = []

    def _check_active(self):
        self.execution._check_thread()
        self.execution._check_healthy()
        if self.execution._active is not self or self.status != "running":
            self.execution._reject("Evidence operation requires the current active stage")

    def output(self, value: object) -> None:
        self._check_active()
        if self.output_ref is not None:
            self.execution._reject("A stage must emit exactly one output")
        if any(not handle.completed for handle in self._handles):
            self.execution._reject("Stage output has unfinished IO calls")
        self.output_ref = self.execution._snapshot(self.spec.name, "output", {
            "schema": "tiger.stage-output.v1", "value": value,
            "io_calls": sorted(self.io_calls, key=lambda call: call["index"]),
        })

    def passthrough(self, reason: str) -> None:
        self._check_active()
        if not self.spec.allow_passthrough:
            self.execution._reject(f"Non-LLM stage cannot pass through: {self.spec.name}")
        if not isinstance(reason, str) or not reason.strip():
            self.execution._reject("Passthrough requires an explicit reason")
        self.reason = reason.strip()


class ExecutionContext:
    def __init__(self, *, run_id: str, run_date: date, paths: RuntimePaths,
                 policy: ExecutionPolicy, store: EvidenceStore, admitted_at: datetime | None = None,
                 authoritative_paper: bool = False, account_version_before: str | None = None,
                 response_observation_contract: str = ""):
        if not isinstance(run_id, str) or not run_id:
            raise ExecutionContractError("run_id is required")
        if not isinstance(run_date, date) or isinstance(run_date, datetime):
            raise ExecutionContractError("run_date must be the frozen admission-market date")
        self.run_id, self.run_date = run_id, run_date
        self.paths, self.policy, self.store = paths, policy, store
        self.model_required = policy.policy_id in MODEL_REQUIRED_POLICIES
        self.intelligence = policy.policy_id == INTELLIGENCE_POLICY_ID
        if type(response_observation_contract) is not str:
            raise ExecutionContractError("Invalid model response observation contract")
        if response_observation_contract:
            from ..llm.json_observation import CONTRACT
            if not self.intelligence or response_observation_contract != CONTRACT:
                raise ExecutionContractError("Invalid model response observation contract")
        self.response_observation_contract = response_observation_contract
        self._model_format_retry_used = False
        self.admitted_at = admitted_at
        self.authoritative_paper = authoritative_paper
        self.account_version_before = account_version_before
        if authoritative_paper and (not self.model_required or admitted_at is None):
            raise ExecutionContractError("Authoritative paper requires the admitted model workflow")
        self.completed_session_close = None
        if self.model_required and admitted_at is not None:
            from ..market_close import completed_session
            from zoneinfo import ZoneInfo

            self.completed_session_close = completed_session(admitted_at)
            if admitted_at.astimezone(ZoneInfo(policy.market_timezone)).date() != run_date:
                raise ExecutionContractError("Admission timestamp differs from the frozen market date")
        self.signal_benchmark_chart = None
        self._specs = daily_stage_specs(self.model_required, authoritative_paper=authoritative_paper,
                                        intelligence=self.intelligence)
        self._cursor = 0
        self._sequence = 0
        self._owner_thread = get_ident()
        self._active: StageScope | None = None
        self._failed = False
        self._contract_failure: str | None = None
        self._observation_failure: StrictObservationError | None = None
        self._previous_stage_ref: dict | None = None
        self._records: list[dict] = []

    def _reject(self, message: str):
        self._contract_failure = message
        self._failed = True
        raise ExecutionContractError(message)

    def _check_thread(self):
        if get_ident() != self._owner_thread:
            self._reject("Only the execution owner thread may write evidence")

    def _check_healthy(self):
        if not self._failed:
            return
        if self._contract_failure is not None:
            raise ExecutionContractError(self._contract_failure)
        if self._observation_failure is not None:
            raise self._observation_failure
        raise EvidenceWriteError("An earlier evidence operation failed")

    def _snapshot(self, stage: str, role: str, value: object) -> dict:
        self._check_thread()
        try:
            ref = dict(self.store.snapshot(stage=stage, role=role, value=value))
            if not all(key in ref for key in ("path", "sha256", "bytes", "schema")):
                raise ValueError("Snapshot store returned an incomplete reference")
            return ref
        except EvidenceWriteError:
            self._failed = True
            raise
        except Exception as exc:
            self._failed = True
            raise EvidenceWriteError("Could not persist execution snapshot") from exc

    def _event(self, scope: StageScope, kind: str, status: str,
               error_type: str | None = None) -> dict:
        self._check_thread()
        event = {
            "schema": "tiger.execution-event.v1", "run_id": self.run_id,
            "sequence": self._sequence + 1, "stage_index": scope.index,
            "stage": scope.spec.name, "kind": kind, "status": status,
            "input_ref": scope.input_ref, "output_ref": scope.output_ref,
            "previous_stage_ref": self._previous_stage_ref,
            "reason": scope.reason or None, "error_type": error_type,
        }
        try:
            ref = dict(self.store.append_event(event))
        except EvidenceWriteError:
            self._failed = True
            raise
        except Exception as exc:
            self._failed = True
            raise EvidenceWriteError("Could not persist execution event") from exc
        self._sequence += 1
        return ref

    @contextmanager
    def step(self, name: str, inputs: object):
        self._check_thread()
        if self._failed:
            self._reject("A failed execution cannot start another stage")
        if self._active is not None:
            self._reject("Stages cannot overlap")
        if self._cursor >= len(self._specs) or self._specs[self._cursor].name != name:
            expected = self._specs[self._cursor].name if self._cursor < len(self._specs) else "<end>"
            self._reject(f"Expected stage {expected}, received {name}")
        spec = self._specs[self._cursor]
        input_ref = self._snapshot(name, "input", inputs)
        scope = StageScope(self, spec, input_ref, self._cursor + 1)
        self._active = scope
        try:
            self._event(scope, "begin", "running")
            try:
                yield scope
                self._check_healthy()
                if scope.output_ref is None:
                    self._reject(f"Stage {name} did not persist an output")
                if self._contract_failure is not None:
                    raise ExecutionContractError(self._contract_failure)
                if self._failed:
                    raise EvidenceWriteError("An earlier evidence operation failed")
            except BaseException as original:
                self._failed = True
                scope.elapsed_ms = (perf_counter() - scope.started) * 1000
                scope.status = "failed"
                try:
                    if self.response_observation_contract and scope.output_ref is None:
                        scope.output_ref = self._snapshot(name, "output", {
                            "schema": "tiger.failed-stage-output/v2",
                            "response_observation_contract": self.response_observation_contract,
                            "io_calls": sorted(scope.io_calls, key=lambda call: call["index"]),
                        })
                    self._event(scope, "end", "failed", type(original).__name__)
                except EvidenceWriteError as evidence_error:
                    raise evidence_error from original
                self._record(scope)
                raise
            scope.elapsed_ms = (perf_counter() - scope.started) * 1000
            status = "passthrough" if scope.reason else "completed"
            self._event(scope, "end", status)
            scope.status = status
            self._record(scope)
            self._previous_stage_ref = scope.output_ref
            self._cursor += 1
        finally:
            self._active = None

    def _record(self, scope: StageScope):
        self._records.append({
            "stage": scope.spec.name, "stage_index": scope.index,
            "status": scope.status, "ms": round(scope.elapsed_ms, 3),
            "reason": scope.reason, "input_ref": scope.input_ref,
            "output_ref": scope.output_ref,
        })

    def begin_io(self, method: str, inputs: object) -> _IOHandle:
        self._check_thread()
        scope = self._active
        if scope is None:
            self._reject("IO evidence requires an active stage")
        scope._check_active()
        if scope.output_ref is not None:
            self._reject("Cannot start IO after stage output")
        if not isinstance(method, str) or not method or any(c in method for c in "\r\n"):
            self._reject("IO method must be a controlled nonempty name")
        handle = _IOHandle(scope, len(scope._handles) + 1, method,
                           self._snapshot(scope.spec.name, "input", inputs))
        scope._handles.append(handle)
        return handle

    def model_attempt_snapshot(self, value: object) -> dict:
        """Persist a native attempt while its exact model IO is still active."""
        self._check_thread()
        scope = self._active
        if not self.response_observation_contract or scope is None:
            self._reject("Native model observation requires its admitted active stage")
        scope._check_active()
        pending = [handle for handle in scope._handles
                   if not handle.completed and handle.method == "model.deepseek.chat"]
        if scope.output_ref is not None or len(pending) != 1:
            self._reject("Native model observation requires one unfinished model IO")
        return self._snapshot(scope.spec.name, "output", value)

    def claim_model_format_retry(self) -> bool:
        """Share the single admitted format reanswer across all client instances."""
        self._check_thread()
        scope = self._active
        if not self.response_observation_contract or scope is None:
            self._reject("Model format reanswer requires its admitted active stage")
        scope._check_active()
        if self._model_format_retry_used:
            return False
        self._model_format_retry_used = True
        return True

    def finish_io(self, handle: _IOHandle, *, output: object = None,
                  error: BaseException | None = None,
                  outcome_observed: bool = True) -> None:
        self._check_thread()
        if not isinstance(handle, _IOHandle) or handle.scope is not self._active:
            self._reject("IO handle belongs to another stage")
        scope = handle.scope
        scope._check_active()
        if handle.completed or scope.output_ref is not None:
            self._reject("IO may finish exactly once before stage output")
        value = ({"error_type": type(error).__name__, "message": "External call failed",
                  "outcome_observed": outcome_observed}
                 if error is not None else output)
        if error is not None and self.model_required and handle.method in MODEL_IO_METHODS:
            diagnostic = model_failure_diagnostic(error)
            if diagnostic is not None:
                value["diagnostic"] = diagnostic
            if self.response_observation_contract:
                from .model_diagnostics import model_failure_observation
                observation = model_failure_observation(error)
                if observation is not None:
                    value["response_observation"] = observation
        ref = self._snapshot(scope.spec.name, "output", value)
        scope.io_calls.append({"index": handle.index, "method": handle.method,
                               "input_ref": handle.input_ref,
                               "error_ref" if error is not None else "output_ref": ref})
        handle.completed = True

    def call_io(self, method: str, inputs: object, fn: Callable[[], Any]) -> Any:
        handle = self.begin_io(method, inputs)
        try:
            result = fn()
        except BaseException as original:
            try:
                self.finish_io(handle, error=original)
            except EvidenceWriteError as evidence_error:
                raise evidence_error from original
            raise
        self.finish_io(handle, output=result)
        return result

    def mark_passthrough(self, name: str, reason: str) -> None:
        if self._active is None or self._active.spec.name != name:
            self._reject("Passthrough may mark only its active matching stage")
        self._active.passthrough(reason)

    def assert_pipeline_complete(self) -> None:
        from . import DEFERRED_STEPS, pipeline_steps
        required = [s.name for s in pipeline_steps(self.intelligence) if s.name not in DEFERRED_STEPS]
        actual = [r["stage"] for r in self._records if r["stage"] in required]
        if self._failed or actual != required:
            self._reject("Pipeline subsequence is incomplete")

    def assert_complete(self) -> None:
        self._check_thread()
        if self._failed or self._active is not None or self._cursor != len(self._specs):
            self._reject("Daily execution is not complete")

    def summary(self) -> list[dict]:
        return [dict(record) for record in self._records]


def market_observation(value, *, run_date: date, market_timezone: str):
    """Validate all observed rows, then present date consumers with NY local time.

    Naive daily indices are market-date labels and retain their dates. Aware
    indices denote instants: convert to the admission market before comparing
    or removing the timezone. The raw observation is persisted by the caller
    before this projection; neither its index nor its prices are mutated.
    Option expiry fields are contract dates, not observation dates.
    """
    import pandas as pd

    if isinstance(value, (pd.DataFrame, pd.Series)):
        if value.empty:
            return value
        if not isinstance(value.index, pd.DatetimeIndex) or value.index.hasnans:
            raise StrictObservationError("Provider observation has unverifiable dates")
        index = value.index
        if index.tz is not None:
            index = index.tz_convert(market_timezone).tz_localize(None)
        if any(day > run_date for day in index.date):
            raise StrictObservationError("Provider observation is later than the frozen market date")
        if value.index.tz is not None:
            projected = value.copy()
            projected.index = index
            return projected
        return value
    if isinstance(value, dict):
        projected = {key: market_observation(item, run_date=run_date,
                                             market_timezone=market_timezone)
                     for key, item in value.items()}
        return value if all(projected[key] is item for key, item in value.items()) else projected
    return value


def daily_macro_observation(value, *, run_date: date, market_timezone: str):
    """Project an explicit daily macro label without changing its calendar date.

    A source-local midnight is a label for a source session, not an intraday
    instant. Converting Chicago midnight to New York produces 01:00; converting
    UTC midnight can even move it to the previous day. Only the two registered
    rate/volatility daily methods use this versioned rule. Validate every raw
    row before dropping its timezone; ordinary aware observations keep their
    original instant semantics. The caller has already persisted the raw return.
    """
    import pandas as pd

    if value is None or isinstance(value, pd.Series) and value.empty:
        return value
    if not isinstance(value, pd.Series):
        raise StrictObservationError("Macro daily feed must be a typed series")
    index = value.index
    if (not isinstance(index, pd.DatetimeIndex) or index.hasnans
            or not index.is_monotonic_increasing or not index.is_unique
            or not index.equals(index.normalize())):
        raise StrictObservationError("Macro daily feed contains unverifiable source-session labels")
    local_labels = index.tz_localize(None) if index.tz is not None else index
    if any(day > run_date for day in local_labels.date):
        raise StrictObservationError("Macro source-session label is later than the frozen market date")
    if value.index.tz is None:
        return value
    projected = value.copy()
    projected.index = local_labels
    return projected


class RecordingProvider:
    """Record public provider calls without changing their internal algorithms."""
    def __init__(self, provider, execution: ExecutionContext):
        from ..providers.base import is_synthetic
        if is_synthetic(provider):
            raise StrictSourceSynthetic("Synthetic provider is forbidden by strict public policy")
        if provider.name not in execution.policy.provider_names:
            raise ExecutionContractError("Provider is outside strict public policy")
        if provider.name == "stooq" and getattr(getattr(provider, "_http", None),
                                                "allow_challenge", True):
            raise ExecutionContractError("Strict Stooq provider must refuse access challenges")
        self._provider = provider
        self.execution = execution
        self.name = provider.name
        self.synthetic = False

    def __getattr__(self, name: str):
        value = getattr(self._provider, name)
        if not callable(value) or name.startswith("_"):
            return value
        def invoke(*args, **kwargs):
            raw = self.execution.call_io(
                f"provider.{self.name}.{name}",
                {"provider": self.name, "method": name, "args": args, "kwargs": kwargs},
                lambda: value(*args, **kwargs),
            )
            try:
                close = self.execution.completed_session_close
                projector = (daily_macro_observation if close is not None and name in {
                    "rate_yield_for", "vol_index_for",
                } else market_observation)
                observed = projector(raw, run_date=self.execution.run_date,
                                     market_timezone=self.execution.policy.market_timezone)
                stage = self.execution._active.spec.name
                if close is not None and stage == "tech.monitor":
                    from ..market_close import close_observation

                    if name == "ohlcv":
                        return close_observation(observed, close.date(), exact=True)
                    if name == "ohlcv_batch" and isinstance(observed, dict):
                        return {symbol: aligned for symbol, frame in observed.items()
                                if (aligned := close_observation(frame, close.date(), exact=True)) is not None}
                return observed
            except StrictObservationError as error:
                # A caught boundary violation still poisons this stage before
                # another provider, ledger write, or output can be started.
                self.execution._observation_failure = error
                self.execution._failed = True
                raise
        return invoke


def record_provider(provider, execution: ExecutionContext | None):
    if execution is None:
        return provider
    if isinstance(provider, RecordingProvider):
        if provider.execution is not execution:
            raise ExecutionContractError("Provider belongs to another execution")
        return provider
    return RecordingProvider(provider, execution)
