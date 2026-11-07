"""Clean formatting guidance keeps frozen request replay and legal windows."""
from datetime import datetime, timezone
from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest

from trading_system.agent_evidence import EvidenceInvalid
from trading_system.agent_runtime import SCENE_POLICY_RELATIVE, SOURCE_ROOT, frozen_clock
from trading_system.llm import intelligence_proof as proof
from trading_system.llm import intelligence_required as semantic
from trading_system.llm.deepseek_required import sanitize_model_text, sanitize_model_value, validate_schema
from trading_system.llm.model_budget import (CONTEXT_TOKENS, MAX_OUTPUT_TOKENS, canonical_json,
    chat_request_core, count_json_tokens, plan_request)
from trading_system.llm.required_schemas import clean_schema
from trading_system.redline import LLMUnavailable
from trading_system.redline.execution import REQUIRED_MODEL_STAGES, RequiredModelFailed
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


@pytest.mark.parametrize("contract,task_sha,wire_sha", [
    (semantic.LEGACY_CLEAN_FORMAT_CONTRACT,
     "fa974aa29c276905fe9a0f6b3226f674eb07579147d77cf9639d381350f58c82",
     "aeadaaa653a29d63083d50e30cf5918dbc6260539db3a3dfa9a65da130eedaf1"),
    (semantic.SUMMARY_CLEAN_FORMAT_CONTRACT,
     "a5537cce004c4ccfc07d4a085f3bccda3d7e664310eabb4090ed54182a8ff275",
     "df23f2feb8964b1f89cd02127ea100bd1b81eedbbf742429052e8a53508f8baf"),
    (semantic.IDENTIFIERS_CLEAN_FORMAT_CONTRACT,
     "77a60f0ca4f35bdaaf1377899494530267fb7a12e2eabb58ad0375f39e686936",
     "87aca1bd0eb73fdece79944f1b0a74dfdee08143d9ccb6ba083904fd847bb667"),
])
def test_all_historical_task_and_complete_wire_hashes_remain_exact(contract, task_sha, wire_sha):
    # Recorded before each version change, using the complete legal window.
    schema = clean_schema([], structured=True, target_ids=semantic.TARGET_IDS)
    arguments = semantic.chat_arguments("clean.llm_semantic",
        {"documents": [], "target_ids": semantic.TARGET_IDS}, schema, assessment(),
        clean_format_contract=contract)
    user = sanitize_model_text(arguments["user"]) + "\n" + json.dumps(
        sanitize_model_value(arguments["public_payload"]), ensure_ascii=False,
        sort_keys=True, separators=(",", ":"), allow_nan=False)
    wire, _estimate, ceiling = plan_request(chat_request_core(system=sanitize_model_text(arguments["system"]),
        user=user, schema=schema, schema_hint={}, thinking_mode="disabled", effort=None, temperature=.2))
    assert hashlib.sha256(arguments["user"].encode()).hexdigest() == task_sha
    assert hashlib.sha256(canonical_json(wire).encode()).hexdigest() == wire_sha
    assert ceiling == MAX_OUTPUT_TOKENS
    assert ("清洗标识使用独立命名空间" in arguments["user"]) == (
        contract == semantic.IDENTIFIERS_CLEAN_FORMAT_CONTRACT)
    assert "本批输入documents恰好" not in arguments["user"]


@pytest.mark.parametrize("constant,expected", [
    ("LEGACY_CLEAN_SOURCE_SHA256", semantic.LEGACY_CLEAN_FORMAT_CONTRACT),
    ("SUMMARY_CLEAN_SOURCE_SHA256", semantic.SUMMARY_CLEAN_FORMAT_CONTRACT),
    ("SNAPSHOT_ONLY_SUMMARY_CLEAN_SOURCE_SHA256", semantic.SUMMARY_CLEAN_FORMAT_CONTRACT),
    ("IDENTIFIERS_CLEAN_SOURCE_SHA256", semantic.IDENTIFIERS_CLEAN_FORMAT_CONTRACT),
])
def test_recognized_historical_source_maps_to_its_own_task_without_execution(tmp_path, monkeypatch, constant, expected):
    # Only the fixture trust digest is substituted. Unknown source rejection is
    # exercised separately; actual 09aa module/wire replay is a delivery probe.
    content = b"raise AssertionError('Historical frozen code must not execute')\n"
    frozen_semantic(tmp_path, content)
    monkeypatch.setattr(proof, constant, hashlib.sha256(content).hexdigest())
    assert proof.frozen_clean_format_contract(tmp_path) == expected


def test_identifier_prompt_distinguishes_targets_and_requires_local_evidenced_subjects():
    assert semantic.IDENTIFIERS_CLEAN_FORMAT_CONTRACT == "typed-clean-identifiers/v1"
    assert semantic.CLEAN_FORMAT_CONTRACT == "typed-clean-complete-format/v1"
    assert proof.SNAPSHOT_ONLY_SUMMARY_CLEAN_SOURCE_SHA256 == (
        "8d3154751d3b92e0024590cb9d2226d68638a59156a81ba0ecd626eec9845222")
    assert proof.IDENTIFIERS_CLEAN_SOURCE_SHA256 == (
        "6cea0fe5533469d8888ecde8e7fd9bb853c7c1dea544136c0dd2655292b61f36")
    task = semantic.chat_arguments("clean.llm_semantic", {"documents": []}, {}, assessment())["user"]
    assert task.startswith(semantic.IDENTIFIERS_CLEAN_TASK)
    for guidance in ("顶层输入target_ids", "裸产业链ID或ETF代码", "global:、chain:、sector:、security:、account:",
                     "event_type=macro或policy", "target_ids=[]", "只引用本篇subjects[].id", "必须非空",
                     "无法从本篇原文确证主体时", "仍保留该document和完整原文", "未解决缺口",
                     "relation=refutes", "不能为通过schema而清空"):
        assert guidance in task


def typed_annotation():
    return {"id": "document", "tickers": [], "sentiment": "neutral", "sentiment_score": 0,
        "events": ["macro"], "summary_zh": "制造业资料存在，主体及时间仍需核验。", "relevance": .5,
        "subjects": [{"id": "subject-1", "type": "industry", "name": "Manufacturing",
            "identifiers": [], "span_ids": ["span-1"]}],
        "evidence_spans": [{"id": "span-1", "quote": "Manufacturing weakened.", "start": 0, "end": 23}],
        "event_records": [{"id": "event-1", "event_type": "macro", "subject_ids": ["subject-1"],
            "is_primary": True, "event_identifiers": [],
            "report_period": {"kind": "unknown", "value": None, "span_ids": []},
            "event_time": {"precision": "unknown", "value": None, "span_ids": []},
            "effective_time": {"precision": "unknown", "value": None, "span_ids": []},
            "source_span_ids": ["span-1"], "claim_kind": "analysis", "target_ids": []}],
        "claims": [{"id": "claim-1", "event_id": "event-1", "predicate": "manufacturing_strength",
            "value": None, "unit": None, "currency": None, "source_span_ids": ["span-1"],
            "relation": "refutes", "conflict_group": None, "supersedes_claim_id": None}]}


@pytest.mark.parametrize("targets", [[], ["memory"], ["XLK"]])
def test_macro_empty_or_given_target_preserves_evidenced_subject_and_counterevidence(targets):
    annotation = typed_annotation()
    annotation["event_records"][0]["target_ids"] = targets
    before = deepcopy(annotation)
    validate_schema({"documents": [annotation]}, clean_schema(["document"], structured=True,
        target_ids=semantic.TARGET_IDS))
    assert annotation == before and annotation["claims"][0]["relation"] == "refutes"


@pytest.mark.parametrize("target", ["global:macro", "chain:memory", "sector:XLK", "security:us:NVDA", "account:holdings"])
def test_acquisition_target_namespace_still_fails_event_enum(target):
    annotation = typed_annotation()
    annotation["event_records"][0]["target_ids"] = [target]
    before = deepcopy(annotation)
    with pytest.raises(LLMUnavailable) as caught:
        validate_schema({"documents": [annotation]}, clean_schema(["document"], structured=True,
            target_ids=semantic.TARGET_IDS))
    assert caught.value.diagnostic["schema_path"] == "/documents/0/event_records/0/target_ids/0"
    assert caught.value.diagnostic["schema_keyword"] == "enum"
    assert annotation == before


def test_empty_event_subject_still_fails_without_output_repair():
    annotation = typed_annotation()
    annotation["event_records"][0]["subject_ids"] = []
    before = deepcopy(annotation)
    with pytest.raises(LLMUnavailable) as caught:
        validate_schema({"documents": [annotation]}, clean_schema(["document"], structured=True,
            target_ids=semantic.TARGET_IDS))
    assert caught.value.diagnostic["schema_path"] == "/documents/0/event_records/0/subject_ids"
    assert caught.value.diagnostic["schema_keyword"] == "minItems"
    assert annotation == before


def test_unresolved_subject_can_keep_document_and_summary_without_fabricating_typed_event():
    annotation = typed_annotation()
    annotation.update(subjects=[], event_records=[], claims=[], summary_zh="正文有制造业反证，但主体未能确证，仍存在未解决缺口。")
    before = deepcopy(annotation)
    validate_schema({"documents": [annotation]}, clean_schema(["document"], structured=True,
        target_ids=semantic.TARGET_IDS))
    assert annotation == before and annotation["id"] == "document"


def test_all237_complete_originals_remain_in_exact_legal_wire(tmp_path):
    frozen_semantic(tmp_path)
    replay = proof._Replay.__new__(proof._Replay)
    replay.run, replay.assessment = tmp_path, assessment()
    replay.scene_policy_path = SOURCE_ROOT / SCENE_POLICY_RELATIVE
    originals = [RawDocument("document-" + str(index), "web_page", "TEST_FIXTURE",
        "https://example.test/" + str(index), "Manufacturing weakened. " * 60 + "TAIL-" + str(index),
        "2026-10-19T12:00:00+00:00", meta={"target_ids": ["global:macro"]}) for index in range(237)]
    batches = semantic.plan_clean_documents(originals, replay.planner("clean.llm_semantic"))
    assert len(batches) == 1
    docs, payload, schema = batches[0]
    assert docs == originals and len(payload["documents"]) == 237
    wire, estimate, ceiling = replay.planner("clean.llm_semantic")(payload, schema)
    assert estimate == count_json_tokens(wire) and estimate + ceiling <= CONTEXT_TOKENS
    assert ceiling == wire["max_tokens"] == min(MAX_OUTPUT_TOKENS, CONTEXT_TOKENS - estimate)
    for original, row in zip(originals, payload["documents"], strict=True):
        assert row["content"] == original.content and row["source_metadata"] == original.meta
        assert row["native_content_sha256"] == hashlib.sha256(original.content.encode()).hexdigest()
        assert original.content in wire["messages"][1]["content"]


@pytest.mark.parametrize("count", [0, 1, 2, 237])
def test_new_format_prompt_uses_actual_batch_count_and_exact_ordered_ids(count):
    documents = [{"id": "document-" + str(index)} for index in range(count)]
    arguments = semantic.chat_arguments("clean.llm_semantic", {"documents": documents}, {}, assessment())
    task = arguments["user"]
    assert f"本批输入documents恰好{count}行，输出documents也必须恰好{count}行" in task
    assert "按输入原顺序逐行输出，第i行id必须逐字等于第i个输入id" in task
    for guidance in ("summary_zh等schema要求纯文本", "不能含字符<或>", "优于、低于",
                     "content、evidence_spans[].quote", "value/unit/currency",
                     "不同来源/版本仍按各自输入id独立输出", "不合并、不去重、不按相关性过滤",
                     "绝不添加_dup", "不能漏行、重复行或额外添行"):
        assert guidance in task
    assert arguments["public_payload"]["documents"] == documents


@pytest.mark.parametrize("documents", [None, {}, (), "not-a-row-list"])
def test_new_format_count_requires_explicit_document_rows(documents):
    with pytest.raises(RequiredModelFailed, match="explicit document row list"):
        semantic.chat_arguments("clean.llm_semantic", {"documents": documents}, {}, assessment())


@pytest.mark.parametrize("summary", ["甲>乙", "甲<乙", "甲\n乙", "甲\u202e乙"])
def test_plaintext_summary_still_rejects_operators_or_controls_without_repair(summary):
    annotation = typed_annotation()
    annotation["summary_zh"] = summary
    before = deepcopy(annotation)
    with pytest.raises(LLMUnavailable) as caught:
        validate_schema({"documents": [annotation]}, clean_schema(["document"], structured=True,
            target_ids=semantic.TARGET_IDS))
    assert caught.value.diagnostic["schema_path"] == "/documents/0/summary_zh"
    assert caught.value.diagnostic["schema_keyword"] == "pattern"
    assert annotation == before


def test_chinese_comparison_accepts_without_rewriting_verbatim_quote_or_claim_literals():
    annotation = typed_annotation()
    annotation["summary_zh"] = "制造业弱于上期，原文还记载了金额条件，仍需核验事实。"
    quote = "Manufacturing weakened. Value>5 USD per share."
    annotation["evidence_spans"][0].update(quote=quote, end=len(quote))
    annotation["claims"][0].update(value="5", unit="per share", currency="USD")
    before = deepcopy(annotation)
    validate_schema({"documents": [annotation]}, clean_schema(["document"], structured=True,
        target_ids=semantic.TARGET_IDS))
    assert annotation == before and annotation["evidence_spans"][0]["quote"] == quote


def test_same_url_or_story_versions_keep_both_original_rows_and_special_characters(tmp_path):
    frozen_semantic(tmp_path)
    replay = proof._Replay.__new__(proof._Replay)
    replay.run, replay.assessment = tmp_path, assessment()
    replay.scene_policy_path = SOURCE_ROOT / SCENE_POLICY_RELATIVE
    content = "Manufacturing weakened. Value>5 USD per share."
    originals = [RawDocument("document-v" + str(index), "web_page", "TEST_FIXTURE same story",
        "https://example.test/same-story", content, "2026-10-19T12:00:00+00:00",
        meta={"intelligence_version": index}) for index in (1, 2)]
    batches = semantic.plan_clean_documents(originals, replay.planner("clean.llm_semantic"))
    assert len(batches) == 1
    docs, payload, schema = batches[0]
    assert docs == originals and [row["id"] for row in payload["documents"]] == ["document-v1", "document-v2"]
    assert [row["content"] for row in payload["documents"]] == [content, content]
    assert [row["source_metadata"]["intelligence_version"] for row in payload["documents"]] == [1, 2]
    wire, _estimate, _ceiling = replay.planner("clean.llm_semantic")(payload, schema)
    assert "本批输入documents恰好2行" in wire["messages"][1]["content"]
    assert "Value>5 USD per share." in wire["messages"][1]["content"]


def test_missing_or_fabricated_ids_and_duplicates_remain_rejected_without_host_completion():
    first, second = typed_annotation(), typed_annotation()
    first["id"], second["id"] = "document-v1", "document-v2"
    schema = clean_schema(["document-v1", "document-v2"], structured=True, target_ids=semantic.TARGET_IDS)
    with pytest.raises(LLMUnavailable):
        validate_schema({"documents": [first]}, schema)
    second["id"] = "document-v2_dup"
    before = deepcopy(second)
    with pytest.raises(LLMUnavailable):
        validate_schema({"documents": [first, second]}, schema)
    assert second == before
    second["id"], second["summary_zh"] = "document-v1", "同一id的另一个不同说明。"
    validate_schema({"documents": [first, second]}, schema)
    with pytest.raises(RequiredModelFailed):
        semantic._coverage([first, second], "id", ["document-v1", "document-v2"])
