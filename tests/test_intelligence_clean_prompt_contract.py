"""Clean formatting guidance keeps frozen request replay and legal windows."""
from datetime import datetime, timezone
import hashlib
from pathlib import Path

import pytest

from trading_system.agent_evidence import EvidenceInvalid
from trading_system.agent_runtime import SCENE_POLICY_RELATIVE, SOURCE_ROOT, frozen_clock
from trading_system.llm import intelligence_proof as proof
from trading_system.llm import intelligence_required as semantic
from trading_system.llm.deepseek_required import validate_schema
from trading_system.llm.required_schemas import clean_schema
from trading_system.redline import LLMUnavailable
from trading_system.redline.execution import REQUIRED_MODEL_STAGES
from trading_system.search.models import RawDocument


def frozen_semantic(tmp_path, content=None):
    path = tmp_path / "source/trading_system/llm/intelligence_required.py"
    path.parent.mkdir(parents=True)
    path.write_bytes(Path(semantic.__file__).read_bytes() if content is None else content)
    return path


def assessment():
    clock = frozen_clock(datetime(2026, 10, 20, 22, tzinfo=timezone.utc))
    return {key: clock[key] for key in ("run_date", "market_timezone")}


def test_historical_clean_prompt_bytes_remain_exactly_replayable():
    # SHA independently extracted from the actual successful 52fc request's
    # user task, before the JSON payload; the original raw request is retained
    # in delivery evidence rather than embedding financial source data here.
    arguments = semantic.chat_arguments("clean.llm_semantic", {"documents": []}, {}, assessment(),
        clean_format_contract=semantic.LEGACY_CLEAN_FORMAT_CONTRACT)
    assert hashlib.sha256(arguments["user"].encode()).hexdigest() == (
        "fa974aa29c276905fe9a0f6b3226f674eb07579147d77cf9639d381350f58c82")
    assert "Unicode" not in arguments["user"]


def test_new_contract_adds_only_format_guidance_and_retains_all_original_requirements():
    arguments = semantic.chat_arguments("clean.llm_semantic", {"documents": []}, {}, assessment())
    assert arguments["user"].startswith(semantic.LEGACY_CLEAN_TASK)
    assert "maxLength=600按Unicode字符数" in arguments["user"]
    assert "160–300字符" in arguments["user"] and "绝不超过600字符" in arguments["user"]
    assert "不能为满足摘要长度去裁切原文证据或改写引用" in arguments["user"]
    assert REQUIRED_MODEL_STAGES == (
        "clean.llm_semantic", "tech.sentiment", "tech.risk", "sector.narrative", "decision.debate")
    with pytest.raises(Exception, match="Unknown clean-format"):
        semantic.chat_arguments("clean.llm_semantic", {}, {}, assessment(), clean_format_contract="unreviewed")


def test_current_frozen_source_selects_recognized_literal_without_execution(tmp_path):
    path = frozen_semantic(tmp_path)
    before = path.read_bytes()
    assert proof.frozen_clean_format_contract(tmp_path) == semantic.CLEAN_FORMAT_CONTRACT
    assert path.read_bytes() == before


@pytest.mark.parametrize("mutation", [
    b"# altered frozen source\n",
    b"raise AssertionError('Frozen code must never execute')\n",
])
def test_unrecognized_or_tampered_frozen_implementation_fails_closed(tmp_path, mutation):
    frozen_semantic(tmp_path, Path(semantic.__file__).read_bytes() + mutation)
    with pytest.raises(EvidenceInvalid) as caught:
        proof.frozen_clean_format_contract(tmp_path)
    assert caught.value.code == "MODEL_REQUIRED_FAILED" and caught.value.stage == "clean.llm_semantic"


@pytest.mark.parametrize("marker", [
    b'CLEAN_FORMAT_CONTRACT = "unreviewed/version"\n',
    b'CLEAN_FORMAT_CONTRACT = str("typed-clean-summary-characters/v1")\n',
    b'CLEAN_FORMAT_CONTRACT = "typed-clean-summary-characters/v1"\nCLEAN_FORMAT_CONTRACT = "typed-clean-summary-characters/v1"\n',
])
def test_literal_version_must_be_single_known_value_even_in_a_matching_trusted_file(tmp_path, monkeypatch, marker):
    path = frozen_semantic(tmp_path, marker)
    monkeypatch.setattr(semantic, "__file__", str(path))
    with pytest.raises(EvidenceInvalid):
        proof.frozen_clean_format_contract(tmp_path)


def test_planning_keeps_full_originals_single_legal_batch_and_complete_output_window(tmp_path):
    frozen_semantic(tmp_path)
    replay = proof._Replay.__new__(proof._Replay)
    replay.run, replay.assessment = tmp_path, assessment()
    replay.scene_policy_path = SOURCE_ROOT / SCENE_POLICY_RELATIVE
    originals = [RawDocument("document-" + str(index), "web_page", "TEST_FIXTURE",
        "https://example.test/" + str(index), ("完整原文。" * 1200) + "TAIL-" + str(index), "2026-10-20")
        for index in range(2)]
    batches = semantic.plan_clean_documents(originals, replay.planner("clean.llm_semantic"))
    assert len(batches) == 1
    docs, payload, schema = batches[0]
    assert docs == originals and len(payload["documents"]) == 2
    for original, row in zip(originals, payload["documents"], strict=True):
        assert row["content"] == original.content and len(row["content"]) > 4000
    wire, _estimate, ceiling = replay.planner("clean.llm_semantic")(payload, schema)
    assert ceiling == wire["max_tokens"] == 393216
    assert all(original.content in wire["messages"][1]["content"] for original in originals)
    assert "maxLength=600按Unicode字符数" in wire["messages"][1]["content"]


def test_guidance_does_not_turn_overlong_model_answer_into_valid_or_truncated_output():
    schema = clean_schema(["document"], structured=True, target_ids=semantic.TARGET_IDS)
    document = {"id": "document", "tickers": [], "sentiment": "neutral", "sentiment_score": 0,
        "events": [], "summary_zh": "长" * 601, "relevance": .5, "subjects": [],
        "evidence_spans": [], "event_records": [], "claims": []}
    with pytest.raises(LLMUnavailable):
        validate_schema({"documents": [document]}, schema)
    assert document["summary_zh"] == "长" * 601
    document["summary_zh"] = "长" * 600
    validate_schema({"documents": [document]}, schema)
