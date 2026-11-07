"""Trusted profile, frozen source and atomic paper-account publication.

Only the supervisor publishes ``current.json``. All business writes before that
point are isolated run-local artifacts. Historical commitment is checked through
the hash-bound chain, never from mutable status files.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from html.parser import HTMLParser
import importlib.metadata
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from .agent_evidence import (
    EvidenceInvalid, atomic_bytes, atomic_json, checked_root, digest, file_ref,
    json_bytes, read_bytes, read_json, safe_path, strict_json, verify_events,
    verify_ref,
)
from .redline.execution import (ExecutionPolicy, daily_stage_contract, REQUIRED_PROFILE,
                                REQUIRED_POLICY_ID, INTELLIGENCE_PROFILE, INTELLIGENCE_POLICY_ID,
                                INTELLIGENCE_SEARCH_SOURCES)
from .tech_chain.risk_evidence import CONTRACT as RISK_EVIDENCE_CONTRACT
from .tech_chain.risk_evidence import SOURCE_PATH as RISK_EVIDENCE_RELATIVE
from .llm.json_observation import CONTRACT as RESPONSE_OBSERVATION_CONTRACT, SOURCE_PATH as RESPONSE_OBSERVATION_RELATIVE

PROFILE = "us-public-paper-v1"
ACTIVE_PROFILE = "us-public-active-paper-v1"
SUPPORTED_PROFILES = frozenset({PROFILE, ACTIVE_PROFILE, REQUIRED_PROFILE, INTELLIGENCE_PROFILE})
ACCOUNT = "public-usd"
POLICY_ID = "tiger.public-paper-policy.v1"
RUN_RE = re.compile(r"run-[0-9a-f]{32}\Z")
KEY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{7,95}\Z")
DEADLINE_SECONDS = 2400
MAX_DEADLINE_SECONDS = 7200
RUNTIME_POLICY_ENV = "TIGER_AGENT_RUNTIME_POLICY_FILE"
RUNTIME_POLICY_SCHEMA = "tiger.agent-runtime-policy/v1"
RUNTIME_INPUT_SCHEMA = "tiger.agent-runtime-input/v1"
MAX_RUNTIME_POLICY_BYTES = 4096
SOURCE_ROOT = Path(__file__).resolve().parents[1]
REQUIRED_ACCOUNT_FILES = frozenset({
    "journal.json", "sim_portfolio.json", "calibration_samples.json",
    "governance_events.jsonl",
})
DEPENDENCIES = ("pandas", "numpy", "yfinance", "requests", "PyYAML", "tokenizers")
SCENE_POLICY_RELATIVE = "governance/bundles/trading/model-policy.yml"
SOURCE_CONTENT_CONTRACT = "native-complete/v1"
LEGACY_SOURCE_CONTENT_CONTRACT = "legacy-excerpts/v1"
EVENT_EVIDENCE_RELATIVE = "trading_system/search/event_identity.py"
EVENT_EVIDENCE_CONTRACT = "point-in-time-event/v1"
COMPANY_EVIDENCE_RELATIVE = "trading_system/llm/company_evidence.py"
COMPANY_EVIDENCE_CONTRACT = "sec-cover-search/v1"
SEC_COMPANY_RELATIVE = "trading_system/llm/sec_identity.py"
SEC_COMPANY_CONTRACT = "sec-submission-original/v2"
PAPER_LEDGER_RELATIVE = "trading_system/paper_ledger.py"
PAPER_LEDGER_CONTRACT = "tiger.paper-execution.v1"
# The last deployed contract used 4,000/1,600-character excerpts. Recognize it
# only from its already frozen source bytes, not a missing marker in new runs.
LEGACY_REQUIRED_SOURCE_SHA256 = "220713e8d783adc4d68512e484c85fc60269335659cdcfa9b118ee990b69eceb"


def validate_runtime_input(value: object, profile: str) -> dict:
    """Validate the complete frozen extension, never an arbitrary policy flag."""
    if (profile != INTELLIGENCE_PROFILE or not isinstance(value, dict)
            or set(value) != {"schema", "profile", "business_deadline_seconds"}
            or value.get("schema") != RUNTIME_INPUT_SCHEMA or value.get("profile") != profile
            or type(value.get("business_deadline_seconds")) is not int
            or not DEADLINE_SECONDS <= value["business_deadline_seconds"] <= MAX_DEADLINE_SECONDS):
        raise EvidenceInvalid("RESULT_INVALID", "Frozen business runtime input is invalid")
    return dict(value)


def startup_runtime_input(profile: str | None = None) -> dict | None:
    """Only a trusted startup file can extend the intelligence-paper wall.

    The two-field file contains no path, credential, model, stage or request
    override. Its path is not persisted or passed to the detached engine.
    """
    location = os.environ.get(RUNTIME_POLICY_ENV)
    if location is None:
        return None
    if profile is not None and profile != INTELLIGENCE_PROFILE:
        raise EvidenceInvalid("INVALID_REQUEST", "Custom business runtime is restricted to the intelligence paper profile")
    try:
        if (not isinstance(location, str) or not location or len(location) > 4096
                or any(ord(char) < 32 for char in location)):
            raise ValueError
        path = Path(location)
        if not path.is_absolute():
            raise ValueError
        value = strict_json(read_bytes(path.parent, path.name, limit=MAX_RUNTIME_POLICY_BYTES))
        if (not isinstance(value, dict) or set(value) != {"schema", "business_deadline_seconds"}
                or value.get("schema") != RUNTIME_POLICY_SCHEMA):
            raise ValueError
        return validate_runtime_input({"schema": RUNTIME_INPUT_SCHEMA, "profile": INTELLIGENCE_PROFILE,
            "business_deadline_seconds": value["business_deadline_seconds"]}, INTELLIGENCE_PROFILE)
    except (OSError, UnicodeError, ValueError, TypeError, RecursionError) as exc:
        raise EvidenceInvalid("RESULT_INVALID", "Trusted business runtime policy is unavailable or invalid") from exc


def frozen_runtime_input(run: Path, profile: str) -> dict | None:
    path = run / "runtime-input.json"
    if not path.exists() and not path.is_symlink():
        return None
    try:
        content = read_bytes(run, "runtime-input.json", limit=MAX_RUNTIME_POLICY_BYTES)
        value = validate_runtime_input(strict_json(content), profile)
        if content != json_bytes(value):
            raise ValueError
        return value
    except (OSError, UnicodeError, ValueError, TypeError, RecursionError) as exc:
        raise EvidenceInvalid("RESULT_INVALID", "Frozen business runtime input is unavailable or invalid") from exc


def policy_manifest(profile: str = PROFILE, *, scene_policy_path: Path | None = None,
                    source_content_contract: str = SOURCE_CONTENT_CONTRACT,
                    company_evidence_contract: str = SEC_COMPANY_CONTRACT,
                    event_contract: str = EVENT_EVIDENCE_CONTRACT,
                    risk_evidence_contract: str = RISK_EVIDENCE_CONTRACT,
                    authoritative_paper: bool = True, intelligence_input: dict | None = None,
                    runtime_input: dict | None = None,
                    response_observation_contract: str | None = None) -> dict:
    if not isinstance(profile, str) or profile not in SUPPORTED_PROFILES:
        raise EvidenceInvalid("INVALID_REQUEST", "Unsupported workflow profile")
    intelligence = profile == INTELLIGENCE_PROFILE
    if response_observation_contract is None:
        response_observation_contract = RESPONSE_OBSERVATION_CONTRACT if intelligence else ""
    if (type(response_observation_contract) is not str
            or response_observation_contract not in {"", RESPONSE_OBSERVATION_CONTRACT}
            or response_observation_contract and not intelligence):
        raise EvidenceInvalid("RESULT_INVALID", "Unknown model response observation contract")
    required = profile in {REQUIRED_PROFILE, INTELLIGENCE_PROFILE}
    active = profile == ACTIVE_PROFILE
    policy_id = (INTELLIGENCE_POLICY_ID if intelligence else REQUIRED_POLICY_ID if required
                 else "tiger.public-active-paper-policy.v1" if active else POLICY_ID)
    execution = ExecutionPolicy(policy_id=policy_id, model_mode="enabled",
                                search_source_names=INTELLIGENCE_SEARCH_SOURCES if intelligence
                                else ("deepseek_search",)) if required else ExecutionPolicy(policy_id=policy_id)
    value = {
        "schema": "tiger.policy.v1", "profile": profile,
        "paper_account": ACCOUNT, "execution_mode": "paper",
        "initial_cash_usd": 100000.0, "market": "us",
        "universe": "extended", "strategy": "evidence-complete-active-v1" if active else "reviewed_source_defaults",
        "use_tuned": False, "real_broker": False,
        "execution": asdict(execution),
        "market_timezone": "America/New_York",
        "date_basis": "admission instant in market timezone",
        "search_collection_budget_seconds": 45.0,
        "search_cleanup_budget_seconds": 15.0,
        "business_deadline_seconds": DEADLINE_SECONDS,
        "access_challenges": "refuse",
        "stages": daily_stage_contract(required, authoritative_paper=required and authoritative_paper,
                                        intelligence=intelligence),
        "limitations": [
            "Public market data with a local paper account; no broker orders.",
            "Inner semantic models are disabled; legal passthrough is disclosed.",
            "Event dates use the frozen built-in calendar plus an optional frozen overlay; no automatic earnings feed.",
            "Ordinary source failures and missing observations remain visible.",
            "Local cooperative processes; no hostile same-user or external rollback anchoring guarantee.",
        ],
    }
    if runtime_input is not None:
        runtime_input = validate_runtime_input(runtime_input, profile)
        value["business_deadline_seconds"] = runtime_input["business_deadline_seconds"]
        value["runtime_input_sha256"] = digest(json_bytes(runtime_input))
    if required:
        if authoritative_paper:
            value["paper_ledger_contract"] = PAPER_LEDGER_CONTRACT
        from yaml import YAMLError
        from .llm.scene_policy import KERNEL_SCENE_MAP, ScenePolicyError, scene_config, validate_coverage
        path = scene_policy_path or SOURCE_ROOT / SCENE_POLICY_RELATIVE
        try:
            if validate_coverage(path):
                raise ScenePolicyError("Missing required scene definitions")
            thinking_policy = {stage: scene_config(scene, path)["thinking"]
                               for stage, scene in KERNEL_SCENE_MAP.items()}
        except (OSError, ValueError, TypeError, AttributeError, YAMLError, ScenePolicyError) as exc:
            raise EvidenceInvalid("RESULT_INVALID", "Required model scene policy is missing or invalid") from exc
        value["model_contract"] = {
            "provider": "deepseek", "model": "deepseek-flash",
            "release": "DeepSeek-V4.1-Flash-1017", "required": True,
            "thinking_mode": "scene-policy",
            "thinking_policy": thinking_policy,
            "context_tokens": 1_048_576,
            "max_output_tokens": 393_216,
            "tokenizer_sha256": "89085f12ef79460ac5f66d1119325ddfc694b4ab209d80bbd81d35f081dc9614",
            "search_mode": "provider_native_search",
            "release_binding": "official-current-alias-mapping",
            "model_mapping_url": "https://api-docs.deepseek.com/quick_start/pricing/",
            "search_documentation_url": "https://api-docs.deepseek.com/guides/search/",
            "model_mapping_observed_date": "2026-10-26",
        }
        if source_content_contract == SOURCE_CONTENT_CONTRACT:
            value["model_contract"]["source_content_contract"] = source_content_contract
        elif source_content_contract != LEGACY_SOURCE_CONTENT_CONTRACT:
            raise EvidenceInvalid("RESULT_INVALID", "Unknown source content contract")
        if company_evidence_contract in {COMPANY_EVIDENCE_CONTRACT, SEC_COMPANY_CONTRACT}:
            value["model_contract"]["company_evidence_contract"] = company_evidence_contract
        elif company_evidence_contract:
            raise EvidenceInvalid("RESULT_INVALID", "Unknown company evidence contract")
        if event_contract == EVENT_EVIDENCE_CONTRACT:
            value["model_contract"]["event_contract"] = event_contract
        elif event_contract:
            raise EvidenceInvalid("RESULT_INVALID", "Unknown event evidence contract")
        if risk_evidence_contract == RISK_EVIDENCE_CONTRACT:
            value["model_contract"]["risk_evidence_contract"] = risk_evidence_contract
        elif risk_evidence_contract:
            raise EvidenceInvalid("RESULT_INVALID", "Unknown risk evidence contract")
        value["limitations"][1] = "All five semantic stages require actual DeepSeek Flash calls; failure stops publication."
        value["limitations"] += [
            "deepseek-flash is a provider alias observed as V4.1-Flash-1017, not an immutable version ID.",
            "Thinking is selected per semantic scene; deterministic extraction is non-thinking while risk, narrative and debate reasoning use explicit effort.",
            "Every request is tokenized with the pinned official DeepSeek tokenizer and receives the complete legal remaining output window.",
            "Search uses DeepSeek native Search API collected by this application, not Flash server-hosted web_search.",
            "Numeric portfolio constraints and accounting remain deterministic; model may veto approved entries, never bypass bounds.",
        ]
        if intelligence:
            from .intelligence_workflow import acquisition_policy, validate_input_packet
            value["intelligence"] = acquisition_policy()
            value["intelligence"]["input_sha256"] = (digest(json_bytes(validate_input_packet(intelligence_input)))
                                                       if intelligence_input is not None else None)
            value["model_contract"].update(
                source_content_contract="intelligence-original-versions/v2",
                event_contract="point-in-time-typed-event/v2",
                risk_evidence_contract="tiger.tech-risk-evidence.v2",
                company_evidence_contract="sec-frozen-typed-original/v3",
                search_mode="intelligence_source_registry")
            if response_observation_contract:
                value["model_contract"]["response_observation_contract"] = response_observation_contract
            value["search_collection_budget_seconds"] = value["intelligence"]["limits"]["deadline_seconds"]
            value["limitations"] += [
                "Intelligence sources are frozen from trusted startup configuration; unavailable sources and unknown version times remain explicit gaps.",
                "Each target has separate typed evidence and coverage; missing research is never a clean-risk score.",
                "A frozen frontier and evidence manifest precede final scoring; unresearched final supplements cannot enter.",
                "Raw historical archives are imported only through an explicit admission snapshot; old semantic outputs are not reused.",
            ]
    if active:
        from .sector_classification import OBSERVED_DATE, SOURCE_RESPONSE_SHA256, SOURCE_URL
        value["sector_proxy_snapshot"] = {"observed_date": OBSERVED_DATE,
            "response_sha256": SOURCE_RESPONSE_SHA256, "source_url": SOURCE_URL,
            "historical_point_in_time": False}
        value["limitations"] += [
            "Activity repairs retain all score/data/risk thresholds; more utilization can still increase realized risk.",
            "Fallback sectors are frozen NASDAQ vendor proxies, not GICS membership or historical point-in-time classifications.",
        ]
    return value


def frozen_clock(instant: datetime | None = None, *, seconds: int = DEADLINE_SECONDS) -> dict:
    if type(seconds) is not int or not DEADLINE_SECONDS <= seconds <= MAX_DEADLINE_SECONDS:
        raise EvidenceInvalid("RESULT_INVALID", "Invalid business runtime duration")
    instant = instant or datetime.now().astimezone()
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise EvidenceInvalid("RESULT_INVALID", "Admission requires an aware timestamp")
    utc = instant.astimezone(timezone.utc)
    market = instant.astimezone(ZoneInfo("America/New_York"))
    return {"admitted_at_utc": utc.isoformat(),
            "host_timestamp": instant.isoformat(),
            "host_offset_seconds": int(instant.utcoffset().total_seconds()),
            "market_timezone": "America/New_York",
            "market_timestamp": market.isoformat(), "run_date": market.date().isoformat(),
            "deadline_at_utc": datetime.fromtimestamp(
                utc.timestamp() + seconds, timezone.utc).isoformat()}


def validate_clock(clock: dict, *, expected_seconds: int = DEADLINE_SECONDS) -> None:
    if not isinstance(clock, dict):
        raise EvidenceInvalid("RESULT_INVALID", "Missing admission clock")
    try:
        host = datetime.fromisoformat(clock["host_timestamp"])
        expected = frozen_clock(host, seconds=expected_seconds)
    except (TypeError, ValueError, KeyError) as exc:
        raise EvidenceInvalid("RESULT_INVALID", "Invalid admission clock") from exc
    if clock != expected:
        raise EvidenceInvalid("RESULT_INVALID", "Admission market date binding differs")


def dependency_identity() -> dict:
    versions = {}
    for name in DEPENDENCIES:
        try:
            distribution = importlib.metadata.distribution(name)
            metadata = distribution.read_text("METADATA")
            versions[name] = {"version": distribution.version,
                              "metadata_sha256": digest((metadata or "").encode())}
        except importlib.metadata.PackageNotFoundError:
            versions[name] = {"version": None, "metadata_sha256": None}
    executable = Path(sys.executable).resolve()
    return {"python_executable": sys.executable, "python_version": sys.version,
            "python_binary_sha256": digest(executable.read_bytes()),
            "packages": versions,
            "scope": "Interpreter bytes and installed package metadata; not a full dependency-file attestation."}


def executable_files(root: Path) -> list[str]:
    paths = {"main.py", "requirements.txt", SCENE_POLICY_RELATIVE}
    for file in (root / "trading_system").rglob("*"):
        if file.is_symlink():
            raise EvidenceInvalid("RESULT_INVALID", "Source symlinks are not supported")
        if file.is_file() and "__pycache__" not in file.parts and file.suffix in {
                ".py", ".json", ".yaml", ".yml", ".csv", ".txt"}:
            paths.add(file.relative_to(root).as_posix())
    for pattern in ("scripts/tiger-agent*.mjs",):
        paths.update(p.relative_to(root).as_posix() for p in root.glob(pattern)
                     if not p.name.endswith((".test.mjs", "-test-support.mjs")))
    for optional in ("events_calendar.json",
                     "bundles/trading/capabilities/workflow-client.mjs",
                     "bundles/trading/agent-workflow.schema.json"):
        if (root / optional).exists():
            paths.add(optional)
    return sorted(paths)


def source_inventory(root: Path) -> list[dict]:
    return [file_ref(root, name, "application/source") for name in executable_files(root)]


def source_tree_hash(files: list[dict]) -> str:
    return digest(json_bytes(files))


def freeze_source(run: Path, source: Path = SOURCE_ROOT) -> dict:
    before = source_inventory(source)
    for ref in before:
        atomic_bytes(run, "source/" + ref["path"], verify_ref(source, ref), exclusive=True)
    after = source_inventory(source)
    if before != after:
        raise EvidenceInvalid("RESULT_INVALID", "Source changed during admission")
    revision, dirty = None, True
    try:
        git_env = {"PATH": os.defpath, "LANG": "C", "GIT_CONFIG_NOSYSTEM": "1"}
        revision = subprocess.run(
            ["/usr/bin/git", "rev-parse", "HEAD"], cwd=source, env=git_env,
            check=True, capture_output=True, text=True, timeout=5).stdout.strip()
        dirty = bool(subprocess.run(
            ["/usr/bin/git", "status", "--porcelain", "--untracked-files=normal"],
            cwd=source, env=git_env, check=True, capture_output=True,
            text=True, timeout=5).stdout.strip())
    except (OSError, subprocess.SubprocessError):
        # A source archive can run, but must never invent a Git integration SHA.
        revision = None
    manifest = {"schema": "tiger.source.v1", "git_commit": revision,
                "git_worktree_dirty": dirty, "tree_sha256": source_tree_hash(before),
                "files": before, "dependencies": dependency_identity()}
    atomic_json(run, "source-manifest.json", manifest, exclusive=True)
    verify_source(run)
    return manifest


def verify_source(run: Path) -> dict:
    manifest = read_json(run, "source-manifest.json")
    if (not isinstance(manifest, dict) or manifest.get("schema") != "tiger.source.v1"
            or not isinstance(manifest.get("files"), list)
            or source_tree_hash(manifest["files"]) != manifest.get("tree_sha256")):
        raise EvidenceInvalid("RESULT_INVALID", "Invalid source manifest")
    frozen = checked_root(run / "source")
    for ref in manifest["files"]:
        verify_ref(frozen, ref, schema="application/source")
    expected = [ref["path"] for ref in manifest["files"]]
    actual = []
    for file in frozen.rglob("*"):
        if file.is_symlink():
            raise EvidenceInvalid("RESULT_INVALID", "Frozen source contains a symlink")
        if file.is_file():
            actual.append(file.relative_to(frozen).as_posix())
    if sorted(actual) != sorted(expected) or len(set(expected)) != len(expected):
        raise EvidenceInvalid("RESULT_INVALID", "Frozen source inventory differs")
    return manifest


def trusted_model_no_proxy(value: str | None) -> str:
    """Only trusted startup may add the fixed model host to loopback bypass."""
    default = "localhost,127.0.0.1,::1"
    if value is None:
        return default
    if type(value) is not str:
        raise EvidenceInvalid("RESULT_INVALID", "Trusted model proxy bypass is invalid")
    values = value.split(",")
    base = {"localhost", "127.0.0.1", "::1"}
    if (len(values) != len(set(values)) or set(values) not in (base, base | {"api.deepseek.com"})):
        raise EvidenceInvalid("RESULT_INVALID", "Trusted model proxy bypass is invalid")
    return default + (",api.deepseek.com" if "api.deepseek.com" in values else "")


def run_environment(work: Path, *, model_required: bool = False) -> dict[str, str]:
    checked_root(work / "tmp", create=True)
    checked_root(work / "cache", create=True)
    env = {
        "PATH": str(Path(sys.executable).parent) + os.pathsep + os.defpath,
        "LANG": "en_US.UTF-8", "LC_ALL": "en_US.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1",
        "PYTHONUNBUFFERED": "1", "OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1",
        "TMPDIR": str(work / "tmp"), "XDG_CACHE_HOME": str(work / "cache"),
        "NETRC": os.devnull, "NO_PROXY": "localhost,127.0.0.1,::1",
    }
    if model_required:
        env["NO_PROXY"] = trusted_model_no_proxy(os.environ.get("NO_PROXY"))
    # Keep HOME unchanged; do not redirect the user's home to an artificial
    # directory. Application configuration comes only from the fixed policy.
    if "HOME" in os.environ:
        env["HOME"] = os.environ["HOME"]
    # Trusted startup configuration only. Business requests cannot supply keys,
    # provider URLs or model overrides. The selected required adapter validates
    # the exact fixed backend/origin/model before using any credential.
    for key in ("LLM_BACKEND", "LLM_BASE_URL", "LLM_MODEL", "LLM_API_KEY", "TIGER_SEC_CONTACT",
                "TIGER_INTEL_BRAVE_API_KEY", "TIGER_INTEL_TAVILY_API_KEY", "TIGER_INTEL_EXA_API_KEY",
                "TIGER_INTEL_FRED_API_KEY", "TIGER_INTEL_SEC_USER_AGENT"):
        if model_required and key in os.environ:
            env[key] = os.environ[key]
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        value = os.environ.get(key) or os.environ.get(key.lower())
        if value:
            parsed = urlsplit(value)
            if (parsed.scheme in {"http", "https", "socks5", "socks5h"}
                    and parsed.hostname in {"127.0.0.1", "::1", "localhost"}
                    and parsed.username is None and parsed.password is None
                    and parsed.path in {"", "/"} and not parsed.query and not parsed.fragment):
                env[key] = value
    return env


def _account_root(state: Path) -> Path:
    return state / "accounts" / ACCOUNT


def pointer_read(state: Path) -> dict | None:
    account = _account_root(state)
    if not (account / "current.json").exists() and not (account / "current.json").is_symlink():
        return None
    pointer = read_json(account, "current.json")
    validate_pointer(pointer)
    return pointer


def validate_pointer(pointer: object) -> None:
    if (not isinstance(pointer, dict)
            or set(pointer) != {"run_id", "version_sha256", "receipt_sha256"}
            or not isinstance(pointer["run_id"], str) or not RUN_RE.fullmatch(pointer["run_id"])
            or any(not isinstance(pointer[key], str) or not re.fullmatch(r"[0-9a-f]{64}", pointer[key])
                   for key in ("version_sha256", "receipt_sha256"))):
        raise EvidenceInvalid("RESULT_INVALID", "Invalid account pointer")


def read_version(state: Path, pointer: dict) -> dict:
    validate_pointer(pointer)
    root = _account_root(state) / "versions" / pointer["run_id"]
    content = read_bytes(root, "version.json")
    if digest(content) != pointer["version_sha256"]:
        raise EvidenceInvalid("EVIDENCE_HASH_MISMATCH", "Account version hash mismatch")
    version = strict_json(content)
    if (not isinstance(version, dict) or set(version) != {
            "schema", "run_id", "account", "parent", "run_date", "files"}
            or version.get("schema") != "tiger.account-version.v1"
            or version.get("run_id") != pointer["run_id"] or version.get("account") != ACCOUNT
            or not isinstance(version.get("files"), list)):
        raise EvidenceInvalid("RESULT_INVALID", "Account version identity differs")
    names = []
    for ref in version["files"]:
        verify_ref(root, ref)
        if not allowed_account_path(ref["path"]):
            raise EvidenceInvalid("RESULT_INVALID", "Unexpected version account path")
        names.append(ref["path"])
    if not REQUIRED_ACCOUNT_FILES.issubset(names) or len(names) != len(set(names)):
        raise EvidenceInvalid("EVIDENCE_MISSING", "Required account files missing or duplicated")
    for file in root.rglob("*"):
        if file.is_symlink():
            raise EvidenceInvalid("RESULT_INVALID", "Unsafe account version")
        if file.is_file() and file.relative_to(root).as_posix() not in names + ["version.json"]:
            raise EvidenceInvalid("RESULT_INVALID", "Unbound account file")
    if version.get("parent") is not None:
        validate_pointer(version["parent"])
    return version


def committed_pointer(state: Path, target: str) -> dict | None:
    pointer = pointer_read(state)
    seen = set()
    found = None
    while pointer is not None:
        if pointer["run_id"] in seen or len(seen) >= 10000:
            raise EvidenceInvalid("RESULT_INVALID", "Invalid account history chain")
        seen.add(pointer["run_id"])
        version = read_version(state, pointer)
        run = state / "runs" / pointer["run_id"]
        receipt_bytes = read_bytes(run, "receipt.json")
        if digest(receipt_bytes) != pointer["receipt_sha256"]:
            raise EvidenceInvalid("EVIDENCE_HASH_MISMATCH", "Account receipt hash mismatch")
        receipt = strict_json(receipt_bytes)
        if (not isinstance(receipt, dict) or receipt.get("schema") != "tiger.receipt.v1"
                or receipt.get("run_id") != pointer["run_id"]
                or receipt.get("account_version_sha256") != pointer["version_sha256"]
                or receipt.get("parent") != version.get("parent")):
            raise EvidenceInvalid("RESULT_INVALID", "Account receipt parent binding differs")
        if pointer["run_id"] == target:
            found = pointer
        pointer = version.get("parent")
    return found


def allowed_account_path(name: str) -> bool:
    return name in REQUIRED_ACCOUNT_FILES or (
        isinstance(name, str)
        and name.startswith(("options_hist/", "review_proposals/"))
        and name.endswith(".json"))


def seed_account() -> dict:
    return {
        "journal.json": [], "calibration_samples.json": [],
        "sim_portfolio.json": {
            "started": None, "initial_cash": 100000.0, "account_currency": "USD",
            "cash": 100000.0, "positions": [], "pending": [], "closed": [],
            "equity_curve": [], "ops_log": [],
        },
    }


def stage_account(state: Path, run: Path, parent: dict | None) -> dict:
    checked_root(run / "work" / "reports", create=True)
    for folder in ("options_hist", "review_proposals"):
        checked_root(run / "work" / "reports" / folder, create=True)
    if parent is None:
        for name, value in seed_account().items():
            atomic_json(run, "work/reports/" + name, value, exclusive=True)
        atomic_bytes(run, "work/reports/governance_events.jsonl", b"", exclusive=True)
    else:
        # Full commitment verification precedes reuse of account data.
        verified = verify_run(state, parent["run_id"], require_committed=True)
        if verified["pointer"] != parent:
            raise EvidenceInvalid("RESULT_INVALID", "Parent commitment changed")
        version = read_version(state, parent)
        source = _account_root(state) / "versions" / parent["run_id"]
        for ref in version["files"]:
            atomic_bytes(run, "work/reports/" + ref["path"],
                         verify_ref(source, ref), exclusive=True)
    refs = account_files(run / "work" / "reports")
    snapshot = {"schema": "tiger.account-input.v1", "account": ACCOUNT,
                "parent": parent, "files": refs}
    atomic_json(run, "account-input.json", snapshot, exclusive=True)
    # Frozen copies independently retain the exact before-state even after the
    # business engine mutates its run-local staged account.
    for ref in refs:
        atomic_bytes(run, "account-before/" + ref["path"],
                     verify_ref(run / "work" / "reports", ref), exclusive=True)
    return snapshot


def account_files(reports: Path) -> list[dict]:
    names = set(REQUIRED_ACCOUNT_FILES)
    for folder in ("options_hist", "review_proposals"):
        directory = reports / folder
        if directory.exists():
            checked_root(directory)
            for path in directory.rglob("*"):
                if path.is_symlink():
                    raise EvidenceInvalid("RESULT_INVALID", "Account symlink refused")
                if path.is_file():
                    if path.suffix != ".json":
                        if path.name.endswith(".lock"):
                            continue
                        raise EvidenceInvalid("RESULT_INVALID", "Unexpected account file")
                    names.add(path.relative_to(reports).as_posix())
    refs = []
    for name in sorted(names):
        schema = "application/x-ndjson" if name.endswith(".jsonl") else "application/json"
        if schema == "application/json":
            read_json(reports, name)
        refs.append(file_ref(reports, name, schema))
    return refs


def materialize_version(state: Path, run: Path, admission: dict) -> dict:
    files = account_files(run / "work" / "reports")
    version_root = _account_root(state) / "versions" / run.name
    checked_root(version_root, create=True)
    for ref in files:
        atomic_bytes(version_root, ref["path"],
                     verify_ref(run / "work" / "reports", ref), exclusive=True)
    version = {"schema": "tiger.account-version.v1", "account": ACCOUNT,
               "run_id": run.name, "parent": admission["parent"],
               "run_date": admission["clock"]["run_date"], "files": files}
    atomic_json(version_root, "version.json", version, exclusive=True)
    atomic_json(run, "account-version.json", version, exclusive=True)
    return version


def _calendar_input(run: Path) -> tuple[bytes, dict]:
    overlay = safe_path(run, "source/events_calendar.json", must_exist=False)
    present = overlay.exists()
    content = (read_bytes(run, "source/events_calendar.json") if present
               else json_bytes({"macro": {}, "earnings": {}}))
    return content, {
        "schema": "tiger.calendar-input.v1",
        "source": "frozen_overlay" if present else "builtin_only",
        "source_path": "source/events_calendar.json" if present else None,
        "work_path": "work/events_calendar.json",
        "sha256": digest(content),
    }


def stage_calendar(run: Path) -> dict:
    """Materialize the optional overlay explicitly; preserve built-in rules."""
    content, record = _calendar_input(run)
    atomic_bytes(run, record["work_path"], content, exclusive=True)
    atomic_json(run, "calendar-input.json", record, exclusive=True)
    return record


def validate_admission(run: Path) -> tuple[dict, dict, dict]:
    admission = read_json(run, "admission.json")
    request = read_json(run, "request.json")
    policy = read_json(run, "policy.json")
    base_fields = {"schema", "run_id", "clock", "parent", "request_sha256", "policy_sha256", "source_sha256"}
    if (not isinstance(admission, dict) or set(admission) not in (
            base_fields, base_fields | {"runtime_input_sha256"})
            or not isinstance(request, dict) or not isinstance(policy, dict)
            or admission.get("schema") != "tiger.admission.v1"
            or admission.get("run_id") != run.name or not RUN_RE.fullmatch(run.name)
            or admission.get("request_sha256") != digest(json_bytes(request))
            or admission.get("policy_sha256") != digest(json_bytes(policy))
            or not isinstance(request.get("profile"), str)
            or request.get("profile") not in SUPPORTED_PROFILES):
        raise EvidenceInvalid("RESULT_INVALID", "Admission policy or request identity differs")
    expected = {"schema_version": "tiger.agent-request/v1", "profile": request["profile"],
                "paper_account": ACCOUNT, "idempotency_key": request.get("idempotency_key")}
    if (request != expected or not isinstance(request.get("idempotency_key"), str)
            or not KEY_RE.fullmatch(request["idempotency_key"])):
        raise EvidenceInvalid("RESULT_INVALID", "Stored request differs from fixed profile")
    runtime_input = frozen_runtime_input(run, request["profile"])
    if (("runtime_input_sha256" in admission) != (runtime_input is not None)
            or runtime_input is not None and admission["runtime_input_sha256"] != digest(json_bytes(runtime_input))):
        raise EvidenceInvalid("EVIDENCE_HASH_MISMATCH", "Admission runtime input binding differs")
    validate_clock(admission["clock"], expected_seconds=(runtime_input["business_deadline_seconds"]
                                                       if runtime_input else DEADLINE_SECONDS))
    source = verify_source(run)
    if admission.get("source_sha256") != digest(read_bytes(run, "source-manifest.json")):
        raise EvidenceInvalid("EVIDENCE_HASH_MISMATCH", "Admission source identity differs")
    required_source = next((ref for ref in source["files"]
                            if ref["path"] == "trading_system/llm/required.py"), None)
    source_content_contract = (LEGACY_SOURCE_CONTENT_CONTRACT if required_source
                               and required_source["sha256"] == LEGACY_REQUIRED_SOURCE_SHA256
                               else SOURCE_CONTENT_CONTRACT)
    frozen_files = {ref["path"] for ref in source["files"]}
    company_evidence_contract = (SEC_COMPANY_CONTRACT if SEC_COMPANY_RELATIVE in frozen_files else
                                 COMPANY_EVIDENCE_CONTRACT if COMPANY_EVIDENCE_RELATIVE in frozen_files else "")
    event_contract = (EVENT_EVIDENCE_CONTRACT
                      if any(ref["path"] == EVENT_EVIDENCE_RELATIVE for ref in source["files"]) else "")
    intelligence_input = None
    if request["profile"] == INTELLIGENCE_PROFILE:
        from .intelligence_workflow import validate_input_packet
        intelligence_input = validate_input_packet(read_json(run, "intelligence-input.json"))
    expected_policy = policy_manifest(request["profile"], source_content_contract=source_content_contract,
                                      event_contract=event_contract,
                                      risk_evidence_contract=RISK_EVIDENCE_CONTRACT
                                      if RISK_EVIDENCE_RELATIVE in frozen_files else "",
                                      company_evidence_contract=company_evidence_contract,
                                      authoritative_paper=any(ref["path"] == PAPER_LEDGER_RELATIVE for ref in source["files"]),
                                      scene_policy_path=run / "source" / SCENE_POLICY_RELATIVE,
                                      intelligence_input=intelligence_input,
                                      runtime_input=runtime_input,
                                      response_observation_contract=(RESPONSE_OBSERVATION_CONTRACT
                                          if RESPONSE_OBSERVATION_RELATIVE in frozen_files
                                          and request["profile"] == INTELLIGENCE_PROFILE else ""))
    if policy != strict_json(json_bytes(expected_policy)):
        raise EvidenceInvalid("RESULT_INVALID", "Admission policy differs from frozen source")
    calendar_content, calendar_record = _calendar_input(run)
    if (read_json(run, "calendar-input.json") != calendar_record
            or read_bytes(run, calendar_record["work_path"]) != calendar_content):
        raise EvidenceInvalid("EVIDENCE_HASH_MISMATCH", "Calendar input differs from its frozen source")
    if admission["parent"] is not None:
        validate_pointer(admission["parent"])
    return admission, policy, source


def _number(value: object) -> bool:
    if not isinstance(value, (float, int)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


class _ReportStructure(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.headings = []
        self.rows = []
        self._heading = None
        self._row = None

    def handle_starttag(self, tag, attrs):
        if tag in {"title", "h1"}:
            self._heading = []
        if tag == "tr":
            self._row = []

    def handle_data(self, data):
        if self._heading is not None:
            self._heading.append(data)
        if self._row is not None:
            self._row.append(data)

    def handle_endtag(self, tag):
        if tag in {"title", "h1"} and self._heading is not None:
            self.headings.append("".join(self._heading))
            self._heading = None
        if tag == "tr" and self._row is not None:
            self.rows.append("".join(self._row))
            self._row = None


def business_summary(run: Path, admission: dict, artifacts: list[dict]) -> dict:
    date = admission["clock"]["run_date"]
    compact = date.replace("-", "")
    required = {
        f"work/reports/result_{compact}.json",
        f"work/reports/日报_{compact}.md", f"work/reports/日报_{compact}.html",
        f"work/reports/复盘_{compact}.md",
    }
    by_name = {ref["path"]: ref for ref in artifacts}
    if len(by_name) != len(artifacts) or not required.issubset(by_name):
        raise EvidenceInvalid("EVIDENCE_MISSING", "Final report artifacts are incomplete")
    for ref in artifacts:
        verify_ref(run, ref)
    reports = checked_root(run / "work" / "reports")
    actual_paths = set()
    for path in reports.rglob("*"):
        if path.is_symlink():
            raise EvidenceInvalid("RESULT_INVALID", "Unsafe report file")
        if path.is_file() and not path.name.endswith(".lock"):
            actual_paths.add(path.relative_to(run).as_posix())
    if actual_paths != set(by_name):
        raise EvidenceInvalid("RESULT_INVALID", "Report artifact inventory differs")
    result = read_json(run, f"work/reports/result_{compact}.json")
    sim = read_json(run, "work/reports/sim_portfolio.json")
    if not isinstance(result, dict) or not isinstance(sim, dict):
        raise EvidenceInvalid("RESULT_INVALID", "Business result is not an object")
    raw = result.get("raw", {})
    if (not isinstance(raw, dict) or result.get("trade_date") != date or raw.get("synthetic") is not False
            or raw.get("account_currency") != "USD"
            or not isinstance(raw.get("currency_policy"), dict)
            or not isinstance(raw.get("accounting"), dict)
            or raw.get("currency_policy", {}).get("quote_currency") != "USD"
            or raw.get("accounting", {}).get("status") != "updated"
            or raw.get("accounting", {}).get("execution_blocked") is not False):
        raise EvidenceInvalid("RESULT_INVALID", "Paper result date/source/currency binding differs")
    freshness = raw.get("freshness")
    if (not isinstance(freshness, dict)
            or not isinstance(freshness.get("benchmark_last_bar"), str)
            or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", freshness["benchmark_last_bar"])
            or freshness["benchmark_last_bar"] > date
            or not isinstance(freshness.get("benchmark_lag_days"), int)
            or isinstance(freshness["benchmark_lag_days"], bool)
            or not 0 <= freshness["benchmark_lag_days"] <= 3
            or not isinstance(raw.get("source_lineage"), list) or not raw["source_lineage"]
            or not isinstance(raw.get("data_issues"), list)
            or not isinstance(raw.get("data_coverage"), str)
            or not re.fullmatch(r"\d+/\d+", raw["data_coverage"])
            or not isinstance(raw.get("scan_stats"), dict)):
        raise EvidenceInvalid("RESULT_INVALID", "Required freshness and data-source disclosures are incomplete")
    profile = read_json(run, "request.json")["profile"]
    scene_policy_path = run / "source" / SCENE_POLICY_RELATIVE
    providers = policy_manifest(profile, scene_policy_path=scene_policy_path)["execution"]["provider_names"]
    for lineage in raw["source_lineage"]:
        if (not isinstance(lineage, list) or len(lineage) != 2
                or not isinstance(lineage[0], str) or not lineage[0]
                or lineage[1] not in providers):
            raise EvidenceInvalid("RESULT_INVALID", "Data-source lineage differs from public policy")
    try:
        benchmark_date = datetime.fromisoformat(freshness["benchmark_last_bar"]).date()
    except ValueError as exc:
        raise EvidenceInvalid("RESULT_INVALID", "Invalid benchmark date") from exc
    from .calendar import prev_trading_day
    expected_date = prev_trading_day(datetime.fromisoformat(date).date())
    if profile in {REQUIRED_PROFILE, INTELLIGENCE_PROFILE}:
        from .market_close import SOURCE_PATH, completed_session, verify_close_evidence
        from .redline.execution import StrictObservationError

        manifest = read_json(run, "source-manifest.json")
        if any(ref["path"] == SOURCE_PATH for ref in manifest["files"]):
            try:
                expected_date = completed_session(datetime.fromisoformat(admission["clock"]["admitted_at_utc"])).date()
            except StrictObservationError as exc:
                raise EvidenceInvalid("RESULT_INVALID", "Unverifiable completed market session") from exc
            if benchmark_date != expected_date:
                raise EvidenceInvalid("RESULT_INVALID", "Benchmark differs from the completed market session")
            verify_close_evidence(run, admission, freshness)
            if any(ref["path"] == "trading_system/security_roster.py" for ref in manifest["files"]):
                from .security_roster import verify_observed_roster
                verify_observed_roster(run, raw, benchmark_date,
                                       datetime.fromisoformat(admission["clock"]["admitted_at_utc"]))
    if freshness["benchmark_lag_days"] != (expected_date - benchmark_date).days:
        raise EvidenceInvalid("RESULT_INVALID", "Benchmark lag and frozen market date disagree")
    from .currency import require_usd_sim_state
    require_usd_sim_state(sim, scope="Verified paper account")
    if (not _number(sim.get("cash")) or not isinstance(sim.get("positions"), list)
            or not isinstance(sim.get("pending"), list)
            or not isinstance(sim.get("equity_curve"), list) or not sim["equity_curve"]
            or not isinstance(sim.get("ops_log"), list) or not sim["ops_log"]):
        raise EvidenceInvalid("RESULT_INVALID", "Paper account is incomplete")
    equity = sim["equity_curve"][-1]
    operation = sim["ops_log"][-1]
    if (not isinstance(equity, dict) or not isinstance(operation, dict)
            or equity.get("date") != date or operation.get("date") != date):
        raise EvidenceInvalid("RESULT_INVALID", "Paper account dates differ")
    value = 0.0
    for position in sim["positions"]:
        if (not isinstance(position, dict) or not _number(position.get("shares")) or position["shares"] <= 0
                or not _number(position.get("last_price")) or position["last_price"] <= 0):
            raise EvidenceInvalid("RESULT_INVALID", "Invalid paper holding valuation")
        value += position["shares"] * position["last_price"]
    actual_equity = round(sim["cash"] + value, 2)
    if (not _number(actual_equity) or not _number(equity.get("equity")) or not _number(operation.get("equity"))
            or abs(equity["equity"] - actual_equity) > 0.011
            or abs(operation["equity"] - actual_equity) > 0.011):
        raise EvidenceInvalid("RESULT_INVALID", "Cash and holdings do not reconcile")
    picks = result.get("picks")
    if not isinstance(picks, list) or (result.get("action") == "AVOID" and picks):
        raise EvidenceInvalid("RESULT_INVALID", "Decision and new signals disagree")
    if (not isinstance(raw.get("redline"), list)
            or any(not isinstance(step, dict) for step in raw["redline"])):
        raise EvidenceInvalid("RESULT_INVALID", "Invalid redline report")
    review = [step for step in raw["redline"] if step.get("step") == "review.daily"]
    if len(review) != 1 or review[0].get("status") != "executed":
        raise EvidenceInvalid("RUN_INCOMPLETE", "Final result does not include completed review")
    markdown = read_bytes(run, f"work/reports/日报_{compact}.md").decode("utf-8")
    memo = read_bytes(run, f"work/reports/复盘_{compact}.md").decode("utf-8")
    markup = read_bytes(run, f"work/reports/日报_{compact}.html").decode("utf-8")
    document = _ReportStructure()
    document.feed(markup)
    if (not markdown.startswith("# ") or date not in markdown.splitlines()[0]
            or not re.search(r"^\|\s*review\.daily\s*\|\s*✅\s*\|", markdown, re.M)
            or not memo.startswith("# 复盘纪要") or date not in memo.splitlines()[0]
            or "## 一、日度归因" not in memo or "## 二、违规六条检测" not in memo
            or not any(date in heading for heading in document.headings)
            or not any("review.daily" in row and "已执行" in row for row in document.rows)
            or "</html>" not in markup.lower()):
        raise EvidenceInvalid("RESULT_INVALID", "Final report date or completed-review content is missing")
    summary = {
        "picks": picks, "positions": sim["positions"], "pending": sim["pending"],
        "cash_usd": sim["cash"], "market_value_usd": round(value, 2),
        "equity_usd": actual_equity, "action": result.get("action"),
        "market_view": result.get("market_view"),
        "watchlist": result.get("watchlist", [])[:5],
        "as_of": {"market_date": date, "market_timezone": "America/New_York",
                  "admitted_at_utc": admission["clock"]["admitted_at_utc"],
                  "benchmark_last_bar": raw.get("freshness", {}).get("benchmark_last_bar"),
                  "freshness": raw.get("freshness")},
        "disclosures": {"synthetic": False, "execution_mode": "paper",
                        "data_coverage": raw.get("data_coverage"),
                        "scan_stats": raw.get("scan_stats"),
                        "data_issues": raw.get("data_issues", []),
                        "source_lineage": raw.get("source_lineage", []),
                        "model_mode": "disabled",
                        "redline": raw.get("redline", []),
                        "account_origin": "independent USD paper account, initially 100000"},
    }
    if profile in {REQUIRED_PROFILE, INTELLIGENCE_PROFILE}:
        if profile == INTELLIGENCE_PROFILE:
            from .llm.intelligence_proof import verify_intelligence_model as verify_required_model
        else:
            from .llm.required_proof import verify_required_model
        from .research_audit import SOURCE_PATH as RESEARCH_AUDIT_SOURCE, verify_audit

        if any(ref["path"] == RESEARCH_AUDIT_SOURCE for ref in read_json(run, "source-manifest.json")["files"]):
            summary["disclosures"]["research_audit"] = verify_audit(run, raw.get("research_audit"))
        policy = read_json(run, "policy.json")
        if policy.get("paper_ledger_contract") == PAPER_LEDGER_CONTRACT:
            from .paper_proof import verify_paper_execution
            summary["disclosures"]["paper_execution"] = verify_paper_execution(run, admission, result, sim)
        model_contract = policy["model_contract"]
        summary["model"] = verify_required_model(run, result, admission, scene_policy_path=scene_policy_path,
            source_content_contract=model_contract.get("source_content_contract", LEGACY_SOURCE_CONTENT_CONTRACT),
            company_evidence_contract=model_contract.get("company_evidence_contract", ""),
            event_contract=model_contract.get("event_contract", ""),
            risk_evidence_contract=model_contract.get("risk_evidence_contract", ""),
            **({"response_observation_contract": model_contract.get("response_observation_contract", "")}
               if profile == INTELLIGENCE_PROFILE else {}))
        if model_contract.get("company_evidence_contract"):
            summary["disclosures"]["company_evidence"] = raw["debate"]["company_evidence"]
        summary["action_summary"] = raw["action_summary"]
        summary["execution_decisions"] = raw["execution_decisions"]
        summary["disclosures"].update(model_mode="enabled", model_required=True)
        if profile == INTELLIGENCE_PROFILE:
            summary["disclosures"]["intelligence"] = raw["intelligence"]
    return summary


def verify_run(state: Path, run_id: str, *, require_committed: bool = True,
               current_source: Path | None = SOURCE_ROOT) -> dict:
    if not isinstance(run_id, str) or not RUN_RE.fullmatch(run_id):
        raise EvidenceInvalid("INVALID_REQUEST", "Invalid run identity")
    run = state / "runs" / run_id
    admission, policy, source = validate_admission(run)
    receipt = read_json(run, "receipt.json")
    if (not isinstance(receipt, dict) or receipt.get("schema") != "tiger.receipt.v1" or receipt.get("run_id") != run_id
            or receipt.get("profile") != policy["profile"] or receipt.get("paper_account") != ACCOUNT):
        raise EvidenceInvalid("RESULT_INVALID", "Receipt identity differs")
    try:
        sealed_at = datetime.fromisoformat(receipt["sealed_at_utc"])
        if (sealed_at.utcoffset() is None
                or sealed_at < datetime.fromisoformat(admission["clock"]["admitted_at_utc"])
                or sealed_at >= datetime.fromisoformat(admission["clock"]["deadline_at_utc"])):
            raise ValueError("Receipt outside admission window")
    except (KeyError, TypeError, ValueError) as exc:
        raise EvidenceInvalid("RESULT_INVALID", "Receipt sealing time differs from admission") from exc
    for name in ("admission", "request", "policy", "source", "events", "engine_result",
                 "account_input", "calendar_input", "account_version", "process_exit", "log_summary", "engine_log"):
        ref = receipt.get(name)
        verify_ref(run, ref)
    expected_paths = {
        "admission": "admission.json", "request": "request.json", "policy": "policy.json",
        "source": "source-manifest.json", "events": "events.jsonl",
        "engine_result": "engine-result.json", "account_input": "account-input.json",
        "calendar_input": "calendar-input.json",
        "account_version": "account-version.json",
        "process_exit": "process-exit.json", "log_summary": "log-summary.json",
        "engine_log": "engine.log",
    }
    configured_runtime = "runtime_input_sha256" in policy
    if ("runtime_input" in receipt) != configured_runtime:
        raise EvidenceInvalid("RESULT_INVALID", "Receipt runtime input binding differs")
    if configured_runtime:
        verify_ref(run, receipt["runtime_input"], schema=RUNTIME_INPUT_SCHEMA)
        expected_paths["runtime_input"] = "runtime-input.json"
    if any(receipt[key]["path"] != path for key, path in expected_paths.items()):
        raise EvidenceInvalid("RESULT_INVALID", "Receipt references unexpected artifacts")
    engine = read_json(run, "engine-result.json")
    if (not isinstance(engine, dict) or engine.get("schema") != "tiger.engine-result.v1"
            or engine.get("run_id") != run_id or engine.get("success") is not True
            or engine.get("error") is not None
            or engine.get("source_tree_sha256") != source["tree_sha256"]
            or not isinstance(engine.get("stage_count"), int) or isinstance(engine["stage_count"], bool)
            or engine.get("stage_count") != len(policy["stages"])
            or not isinstance(engine.get("module_origins"), dict)):
        raise EvidenceInvalid("RUN_INCOMPLETE", "Engine did not finish successfully")
    origins = engine["module_origins"]
    source_paths = {ref["path"] for ref in source["files"]}
    required_modules = {"tiger_frozen_main", "trading_system",
                        "trading_system.agent_supervisor", "trading_system.redline.execution"}
    if not required_modules.issubset(origins):
        raise EvidenceInvalid("RESULT_INVALID", "Engine application origin set is incomplete")
    for module, path in origins.items():
        expected = ("main.py",) if module == "tiger_frozen_main" else (
            module.replace(".", "/") + ".py", module.replace(".", "/") + "/__init__.py")
        if path not in source_paths or path not in expected:
            raise EvidenceInvalid("RESULT_INVALID", "Engine module origin differs")
    process = read_json(run, "process-exit.json")
    if (not isinstance(process, dict) or set(process) != {
            "schema", "run_id", "guardian_pid", "guardian_exit", "engine_pid",
            "engine_exit", "group_cleanup_before_reap"}
            or process["schema"] != "tiger.process-exit.v1" or process["run_id"] != run_id
            or any(type(process[key]) is not int for key in ("guardian_exit", "engine_exit"))
            or process["guardian_exit"] != 0 or process["engine_exit"] != 0
            or process["group_cleanup_before_reap"] is not True
            or any(not isinstance(process[key], int) or isinstance(process[key], bool)
                   or process[key] <= 1 for key in ("guardian_pid", "engine_pid"))):
        raise EvidenceInvalid("RUN_INCOMPLETE", "Clean independent process exit is not proven")
    log = read_json(run, "log-summary.json")
    log_size = len(read_bytes(run, "engine.log"))
    if (not isinstance(log, dict) or log.get("retained_bytes") != log_size
            or not isinstance(log.get("limit_bytes"), int) or log_size > log["limit_bytes"]
            or log.get("diagnostics_are_not_business_receipts") is not True):
        raise EvidenceInvalid("RESULT_INVALID", "Bounded diagnostic log differs")
    stages = verify_events(run, run_id, policy["stages"])
    if receipt.get("event_tail_sha256") != stages["last_event_sha256"]:
        raise EvidenceInvalid("EVIDENCE_HASH_MISMATCH", "Receipt event tail differs")
    status = "completed_degraded" if stages["degraded"] else "completed"
    if receipt.get("status") != status:
        raise EvidenceInvalid("RESULT_INVALID", "Receipt completion state differs")
    before = read_json(run, "account-input.json")
    if (not isinstance(before, dict) or set(before) != {"schema", "account", "parent", "files"}
            or before.get("schema") != "tiger.account-input.v1"
            or before.get("parent") != admission["parent"] or before.get("account") != ACCOUNT
            or not isinstance(before.get("files"), list)):
        raise EvidenceInvalid("RESULT_INVALID", "Account before-state parent differs")
    before_names = []
    for ref in before["files"]:
        verify_ref(run / "account-before", ref)
        before_names.append(ref["path"])
        if not allowed_account_path(ref["path"]):
            raise EvidenceInvalid("RESULT_INVALID", "Unexpected before-state account file")
    if (not REQUIRED_ACCOUNT_FILES.issubset(before_names)
            or len(before_names) != len(set(before_names))):
        raise EvidenceInvalid("RESULT_INVALID", "Before-state account inventory differs")
    if admission["parent"] is None:
        if set(before_names) != REQUIRED_ACCOUNT_FILES:
            raise EvidenceInvalid("RESULT_INVALID", "Genesis account contains extra state")
        for name, value in seed_account().items():
            if read_bytes(run / "account-before", name) != json_bytes(value):
                raise EvidenceInvalid("RESULT_INVALID", "Genesis seed differs")
        if read_bytes(run / "account-before", "governance_events.jsonl") != b"":
            raise EvidenceInvalid("RESULT_INVALID", "Genesis governance is not empty")
    elif before["files"] != read_version(state, admission["parent"])["files"]:
        raise EvidenceInvalid("RESULT_INVALID", "Before-state differs from committed parent")
    version = read_json(run, "account-version.json")
    version_hash = digest(read_bytes(run, "account-version.json"))
    pointer = {"run_id": run_id, "version_sha256": version_hash,
               "receipt_sha256": digest(read_bytes(run, "receipt.json"))}
    actual_version = read_version(state, pointer)
    if (version != actual_version or version["parent"] != admission["parent"]
            or version["run_date"] != admission["clock"]["run_date"]
            or receipt.get("parent") != admission["parent"]
            or receipt.get("account_version_sha256") != version_hash):
        raise EvidenceInvalid("RESULT_INVALID", "Account version binding differs")
    for ref in version["files"]:
        verify_ref(run / "work" / "reports", ref)
    summary = business_summary(run, admission, receipt["artifacts"])
    if receipt["artifacts"] != engine.get("artifacts") or receipt.get("summary") != summary:
        raise EvidenceInvalid("RESULT_INVALID", "Receipt and actual business artifacts differ")
    if require_committed and committed_pointer(state, run_id) != pointer:
        raise EvidenceInvalid("RUN_INCOMPLETE", "Run has no published account commitment")
    limitations = list(policy["limitations"])
    if require_committed:
        durability_file = run / "publication-durability.json"
        if durability_file.exists():
            durability = read_json(run, "publication-durability.json")
            if (not isinstance(durability, dict) or set(durability) != {
                    "schema", "run_id", "pointer", "directory_fsync_confirmed"}
                    or durability["schema"] != "tiger.publication-durability.v1"
                    or durability["run_id"] != run_id or durability["pointer"] != pointer
                    or not isinstance(durability["directory_fsync_confirmed"], bool)):
                raise EvidenceInvalid("RESULT_INVALID", "Publication durability record differs")
            if not durability["directory_fsync_confirmed"]:
                limitations.append("Account publication is visible; its directory fsync reported an error.")
        else:
            limitations.append("Account publication is visible; post-publication durability confirmation is unavailable.")
    is_current = None
    if current_source is not None:
        is_current = source_tree_hash(source_inventory(current_source)) == source["tree_sha256"]
    refs = [receipt[key] for key in expected_paths]
    refs += receipt["artifacts"]
    refs.append(file_ref(run, "receipt.json", "tiger.receipt.v1"))
    refs = list({ref["path"]: ref for ref in refs}.values())
    return {"status": status, "summary": summary, "pointer": pointer,
            "current_source_matches": is_current,
            "source": {"git_commit": source["git_commit"],
                       "tree_sha256": source["tree_sha256"],
                       "git_worktree_dirty": source["git_worktree_dirty"]},
            "verification": {"ok": True, "receipt_sha256": pointer["receipt_sha256"],
                             "evidence_count": len(stages["evidence_refs"]) + len(refs),
                             "checks": ["admission-policy-date", "frozen-source-files",
                                        "ordered-stage-hash-chain", "all-stage-and-IO-snapshots",
                                        "final-artifacts", "cash-holdings-arithmetic",
                                        "immutable-account-version",
                                        "published-account-ancestry" if require_committed
                                        else "prepared-unpublished-account"]},
            "evidence_refs": refs, "limitations": limitations,
            "degraded": stages["degraded"]}


def seal_and_publish(state: Path, run: Path, *, deadline: float | None = None) -> dict:
    admission, policy, _ = validate_admission(run)
    def before_publish():
        if ((deadline is not None and time.monotonic() >= deadline)
                or datetime.now(timezone.utc) >= datetime.fromisoformat(admission["clock"]["deadline_at_utc"])):
            raise TimeoutError("Business deadline elapsed before account publication")
    def check_account_parent():
        if pointer_read(state) != admission["parent"]:
            code = "ACCOUNT_VERSION_CONFLICT" if policy.get("paper_ledger_contract") else "RESULT_INVALID"
            raise EvidenceInvalid(code, "Account parent changed before publication")

    before_publish()
    check_account_parent()
    engine = read_json(run, "engine-result.json")
    if engine.get("run_id") != run.name or engine.get("success") is not True:
        raise EvidenceInvalid("RUN_INCOMPLETE", "Engine result is not successful")
    stages = verify_events(run, run.name, policy["stages"])
    summary = business_summary(run, admission, engine["artifacts"])
    materialize_version(state, run, admission)
    receipt = {
        "schema": "tiger.receipt.v1", "run_id": run.name, "profile": policy["profile"],
        "sealed_at_utc": datetime.now(timezone.utc).isoformat(),
        "paper_account": ACCOUNT,
        "status": "completed_degraded" if stages["degraded"] else "completed",
        "parent": admission["parent"],
        "account_version_sha256": digest(read_bytes(run, "account-version.json")),
        "event_tail_sha256": stages["last_event_sha256"],
        "summary": summary, "artifacts": engine["artifacts"],
    }
    for name, path, schema in (
        ("admission", "admission.json", "tiger.admission.v1"),
        ("request", "request.json", "tiger.agent-request/v1"),
        ("policy", "policy.json", "tiger.policy.v1"),
        ("source", "source-manifest.json", "tiger.source.v1"),
        ("events", "events.jsonl", "application/x-ndjson"),
        ("engine_result", "engine-result.json", "tiger.engine-result.v1"),
        ("account_input", "account-input.json", "tiger.account-input.v1"),
        ("calendar_input", "calendar-input.json", "tiger.calendar-input.v1"),
        ("account_version", "account-version.json", "tiger.account-version.v1"),
        ("process_exit", "process-exit.json", "tiger.process-exit.v1"),
        ("log_summary", "log-summary.json", "application/json"),
        ("engine_log", "engine.log", "text/plain"),
    ):
        receipt[name] = file_ref(run, path, schema)
    if "runtime_input_sha256" in policy:
        receipt["runtime_input"] = file_ref(run, "runtime-input.json", RUNTIME_INPUT_SCHEMA)
    atomic_json(run, "receipt.json", receipt, exclusive=True)
    prepared = verify_run(state, run.name, require_committed=False, current_source=None)
    check_account_parent()
    before_publish()
    durable = True
    try:
        atomic_json(_account_root(state), "current.json", prepared["pointer"],
                    before_publish=before_publish)
    except OSError:
        # Replace can succeed before directory fsync fails. A visible pointer
        # may already be committed; never create a second business mutation.
        if pointer_read(state) != prepared["pointer"]:
            raise
        durable = False
    if pointer_read(state) != prepared["pointer"]:
        raise EvidenceInvalid("RESULT_INVALID", "Account pointer readback differs")
    try:
        atomic_json(run, "publication-durability.json", {
            "schema": "tiger.publication-durability.v1", "run_id": run.name,
            "pointer": prepared["pointer"], "directory_fsync_confirmed": durable,
        }, exclusive=True)
    except OSError:
        # Publication already happened. A read-only verifier explicitly reports
        # missing durability confirmation instead of inventing a rollback.
        pass
    return verify_run(state, run.name, current_source=None)
