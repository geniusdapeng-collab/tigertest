"""Document-keyed clean output preserves semantic constraints and full sources."""
from copy import deepcopy
from datetime import date
import hashlib
import json
from types import SimpleNamespace

import pytest

from trading_system.llm import intelligence_required as semantic
from trading_system.llm.clean_keyed_codec import (
    CONTRACT, build_inline_schema, document_ids, flatten_strict_output,
)
from trading_system.llm.deepseek_required import (
    sanitize_model_text, sanitize_model_value, validate_schema,
)
from trading_system.llm.model_budget import (
    MAX_OUTPUT_TOKENS, TokenBudgetError, canonical_json, chat_request_core, plan_request,
)
from trading_system.llm.required_schemas import clean_schema
from trading_system.redline import LLMUnavailable
from trading_system.redline.execution import RequiredModelFailed
from trading_system.search.models import RawDocument


ASSESSMENT = {"run_date": "2026-10-20", "market_timezone": "America/New_York"}


def annotation():
    return {"tickers": [], "sentiment": "neutral", "sentiment_score": 0,
        "events": ["macro"], "summary_zh": "制造业弱于前期，仍存在未核验的主体及时间缺口。", "relevance": .5,
        "subjects": [{"id": "subject-1", "type": "industry", "name": "Manufacturing",
            "identifiers": [], "span_ids": ["span-1"]}],
        "evidence_spans": [{"id": "span-1", "quote": "Manufacturing weakened. Value>5 USD.",
            "start": None, "end": None}],
        "event_records": [{"id": "event-1", "event_type": "macro", "subject_ids": ["subject-1"],
            "is_primary": True, "event_identifiers": [],
            "report_period": {"kind": "unknown", "value": None, "span_ids": []},
            "event_time": {"precision": "unknown", "value": None, "span_ids": []},
            "effective_time": {"precision": "unknown", "value": None, "span_ids": []},
            "source_span_ids": ["span-1"], "claim_kind": "analysis", "target_ids": ["memory"]}],
        "claims": [{"id": "claim-1", "event_id": "event-1", "predicate": "manufacturing_strength",
            "value": "5", "unit": None, "currency": "USD", "source_span_ids": ["span-1"],
            "relation": "refutes", "conflict_group": None, "supersedes_claim_id": None}]}


def schema(identifiers=("document-a", "document-b")):
    return clean_schema(list(identifiers), structured=True, target_ids=semantic.TARGET_IDS)


def keyed(identifiers=("document-a", "document-b")):
    return {"documents": {identifier: {"id": identifier, "annotation": annotation()}
                           for identifier in identifiers}}


def test_inline_schema_keeps_every_original_annotation_constraint_and_singleton_ids():
    final = schema()
    before = deepcopy(final)
    inline = build_inline_schema(final)
    entries = inline["properties"]["documents"]
    assert inline["additionalProperties"] is False and inline["required"] == ["documents"]
    assert entries["additionalProperties"] is False
    assert entries["required"] == document_ids(final) == ["document-a", "document-b"]
    original = final["properties"]["documents"]["items"]
    for identifier, entry in entries["properties"].items():
        assert entry["additionalProperties"] is False and entry["required"] == ["id", "annotation"]
        assert entry["properties"]["id"] == {"type": "string", "enum": [identifier]}
        annot = entry["properties"]["annotation"]
        assert annot == {"type": "object", "additionalProperties": False,
            "properties": {key: value for key, value in original["properties"].items() if key != "id"},
            "required": [key for key in original["required"] if key != "id"]}
    assert final == before
    entries["properties"]["document-a"]["properties"]["annotation"]["properties"]["summary_zh"]["maxLength"] = 1
    assert entries["properties"]["document-b"]["properties"]["annotation"]["properties"]["summary_zh"]["maxLength"] == 600
    assert final == before and "$ref" not in canonical_json(inline)


def test_flatten_validates_both_complete_schemas_and_only_reorders_explicit_model_values():
    final = schema()
    value = keyed(("document-b", "document-a"))
    before = deepcopy(value)
    calls = []

    def recording_validate(output, contract):
        calls.append((deepcopy(output), deepcopy(contract)))
        validate_schema(output, contract)

    result = flatten_strict_output(value, final, validate=recording_validate)
    assert len(calls) == 2 and calls[0] == (value, build_inline_schema(final))
    assert calls[1] == (result, final)
    assert result == {"documents": [{"id": identifier, **value["documents"][identifier]["annotation"]}
                                    for identifier in ("document-a", "document-b")]}
    assert value == before and result["documents"][0]["claims"][0]["relation"] == "refutes"
    assert result["documents"][0]["evidence_spans"][0]["quote"].endswith("Value>5 USD.")
    assert result["documents"][0]["event_records"][0]["report_period"]["value"] is None
    result["documents"][0]["claims"][0]["value"] = "changed after return"
    assert value == before


@pytest.mark.parametrize("mutation", [
    "missing-document", "extra-document", "renamed-document", "wrong-explicit-id", "missing-id",
    "missing-annotation", "extra-wrapper-field", "annotation-id", "missing-summary", "missing-null",
    "overlong-summary", "plain-text-operator", "empty-subject", "foreign-target", "duplicate-ticker",
    "too-many-events", "wrong-number", "foreign-field", "array-container",
])
def test_invalid_keyed_answers_fail_without_adding_repairing_or_dropping_values(mutation):
    value = keyed()
    entry = value["documents"]["document-a"]
    annot = entry["annotation"]
    if mutation == "missing-document":
        del value["documents"]["document-b"]
    elif mutation == "extra-document":
        value["documents"]["document-extra"] = {"id": "document-extra", "annotation": annotation()}
    elif mutation == "renamed-document":
        value["documents"]["document-a_dup"] = value["documents"].pop("document-a")
    elif mutation == "wrong-explicit-id":
        entry["id"] = "document-b"
    elif mutation == "missing-id":
        del entry["id"]
    elif mutation == "missing-annotation":
        del entry["annotation"]
    elif mutation == "extra-wrapper-field":
        entry["host-default"] = None
    elif mutation == "annotation-id":
        annot["id"] = "document-a"
    elif mutation == "missing-summary":
        del annot["summary_zh"]
    elif mutation == "missing-null":
        del annot["claims"][0]["unit"]
    elif mutation == "overlong-summary":
        annot["summary_zh"] = "长" * 601
    elif mutation == "plain-text-operator":
        annot["summary_zh"] = "甲>乙"
    elif mutation == "empty-subject":
        annot["event_records"][0]["subject_ids"] = []
    elif mutation == "foreign-target":
        annot["event_records"][0]["target_ids"] = ["global:macro"]
    elif mutation == "duplicate-ticker":
        annot["tickers"] = ["AAPL", "AAPL"]
    elif mutation == "too-many-events":
        annot["events"] = ["macro"] * 7
    elif mutation == "wrong-number":
        annot["sentiment_score"] = 1.1
    elif mutation == "foreign-field":
        value["proof"] = "unrequested"
    elif mutation == "array-container":
        value["documents"] = [{"id": "document-a", **annot}]
    before = deepcopy(value)
    with pytest.raises(LLMUnavailable):
        flatten_strict_output(value, schema(), validate=validate_schema)
    assert value == before


def test_empty_input_uses_exact_empty_keyed_container_and_final_empty_array():
    final = schema(())
    assert document_ids(final) == []
    assert build_inline_schema(final)["properties"]["documents"] == {
        "type": "object", "properties": {}, "required": [], "additionalProperties": False}
    assert flatten_strict_output({"documents": {}}, final, validate=validate_schema) == {"documents": []}
    with pytest.raises(LLMUnavailable):
        flatten_strict_output({"documents": {"extra": {"id": "extra", "annotation": annotation()}}},
                              final, validate=validate_schema)


@pytest.mark.parametrize("mutation", [
    "root-open", "root-extra", "row-open", "row-optional", "duplicate-required", "wrong-cardinality",
    "bool-cardinality", "duplicate-enum", "missing-id", "reference", "empty-id-enum",
])
def test_unrecognized_final_schema_cannot_weaken_codec_contract(mutation):
    final = schema()
    documents = final["properties"]["documents"]
    row = documents["items"]
    if mutation == "root-open":
        final["additionalProperties"] = True
    elif mutation == "root-extra":
        final["properties"]["extra"] = {"type": "string"}
        final["required"].append("extra")
    elif mutation == "row-open":
        row["additionalProperties"] = True
    elif mutation == "row-optional":
        row["required"].remove("claims")
    elif mutation == "duplicate-required":
        row["required"].append("claims")
    elif mutation == "wrong-cardinality":
        documents["maxItems"] = 3
    elif mutation == "bool-cardinality":
        documents["minItems"] = documents["maxItems"] = True
    elif mutation == "duplicate-enum":
        row["properties"]["id"]["enum"] = ["document-a", "document-a"]
    elif mutation == "missing-id":
        del row["properties"]["id"]
        row["required"].remove("id")
    elif mutation == "reference":
        row["properties"]["claims"]["items"] = {"$ref": "#/$def/claim"}
    elif mutation == "empty-id-enum":
        row["properties"]["id"]["enum"] = []
    before = deepcopy(final)
    with pytest.raises(ValueError, match="INVALID_CLEAN_FINAL_SCHEMA"):
        build_inline_schema(final)
    assert final == before


def test_new_protocol_binds_exact_source_rows_and_leaves_all_other_stages_unmarked():
    inputs = {"documents": [{"id": "document-a", "content": "unchanged>fact", "source_metadata": {
        "normalization": {"nested": ["complete", None]}}}], "target_ids": semantic.TARGET_IDS}
    before = deepcopy(inputs)
    args = semantic.chat_arguments("clean.llm_semantic", inputs, schema(("document-a",)), ASSESSMENT)
    assert args["clean_protocol"] == {"contract": CONTRACT} and CONTRACT == semantic.CLEAN_FORMAT_CONTRACT
    assert args["schema"] == schema(("document-a",))
    assert args["public_payload"] == {"assessment": ASSESSMENT, **inputs} and inputs == before
    for text in ("顶层documents必须是对象，不是数组", "零输入返回documents={}",
                 "不能省略nullable字段", "不合并、不去重、不按相关性过滤", "绝不添加_dup"):
        assert text in args["user"]
    assert "零输入返回documents=[]" not in args["user"]
    assert "clean_protocol" not in semantic.chat_arguments("tech.risk", {"documents": []}, {}, ASSESSMENT)


@pytest.mark.parametrize("rows,final", [
    ([{"id": "document-b"}, {"id": "document-a"}], schema()),
    ([{"id": "document-a"}], schema()),
    ([{"id": "document-a"}, {"id": "document-a"}], schema()),
    ([{"id": "document-a_dup"}], schema(("document-a",))),
    ([None], schema(("document-a",))),
    ([], {}),
])
def test_new_protocol_rejects_missing_reordered_or_fabricated_input_identity(rows, final):
    with pytest.raises(RequiredModelFailed):
        semantic.chat_arguments("clean.llm_semantic", {"documents": rows}, final, ASSESSMENT)


def test_full_source_bisection_happens_only_after_explicit_context_overflow():
    originals = [RawDocument("document-" + str(index), "web_page", "TEST_FIXTURE",
        "https://example.test/same-story", "完整原文>事实。TAIL-" + str(index), "2026-10-20",
        meta={"version": index, "deep": {"complete": [None, "must stay"]}}) for index in range(5)]
    calls = []

    def plan(inputs, final):
        args = semantic.chat_arguments("clean.llm_semantic", inputs, final, ASSESSMENT)
        calls.append(deepcopy(args))
        if len(inputs["documents"]) > 2:
            raise TokenBudgetError("INPUT_CONTEXT_EXCEEDED")
        return {"planned": True}

    batches = semantic.plan_clean_documents(originals, plan)
    assert [len(batch) for batch, _, _ in batches] == [2, 1, 2]
    flattened = [raw for batch, _, _ in batches for raw in batch]
    assert flattened == originals and len(calls) == 5
    for original, row in zip(originals, [row for _, inputs, _ in batches for row in inputs["documents"]], strict=True):
        assert row["content"] == original.content and row["source_metadata"] == original.meta
        assert row["native_content_sha256"] == hashlib.sha256(original.content.encode()).hexdigest()
    with pytest.raises(TokenBudgetError, match="TOKENIZER_UNAVAILABLE"):
        semantic.plan_clean_documents(originals, lambda *_: (_ for _ in ()).throw(TokenBudgetError("TOKENIZER_UNAVAILABLE")))
    with pytest.raises(RequiredModelFailed, match="full source document"):
        semantic.plan_clean_documents(originals[:1], lambda *_: (_ for _ in ()).throw(TokenBudgetError("INPUT_CONTEXT_EXCEEDED")))


def test_semantic_dispatch_passes_marker_and_revalidates_projected_final_output():
    calls = []
    execution = SimpleNamespace(run_date=date(2026, 10, 20),
                                policy=SimpleNamespace(market_timezone="America/New_York"))

    class Client:
        def plan_chat(self, **kwargs):
            calls.append(kwargs)
            return {"planned": True}

        def complete_json(self, **kwargs):
            calls.append(kwargs)
            return flatten_strict_output(keyed(), kwargs["schema"], validate=validate_schema)

    client = Client()
    client.execution = execution
    inputs = {"documents": [{"id": "document-a"}, {"id": "document-b"}]}
    semantic._stage_plan(client, "clean.llm_semantic")(inputs, schema())
    result = semantic._call(client, "clean.llm_semantic", inputs, schema())
    assert len(result["documents"]) == 2
    assert all(call["clean_protocol"] == {"contract": CONTRACT} for call in calls)
    client.complete_json = lambda **_: {"documents": []}
    with pytest.raises(RequiredModelFailed, match="transport or validation"):
        semantic._call(client, "clean.llm_semantic", inputs, schema())


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
    (semantic.COMPLETE_CLEAN_FORMAT_CONTRACT,
     "35c481eb6ee798c20fc5bfcbb08dff42729233edd15f9fd74f8f4f11cd08fc6c",
     "3d67e49a25258d0baf9baba733fa87023154ca0133ae623aa25972ad2333e127"),
])
def test_four_historical_contracts_keep_exact_tasks_full_wires_and_no_strict_marker(contract, task_sha, wire_sha):
    final = schema(())
    args = semantic.chat_arguments("clean.llm_semantic", {"documents": [], "target_ids": semantic.TARGET_IDS},
                                   final, ASSESSMENT, clean_format_contract=contract)
    assert "clean_protocol" not in args
    user = sanitize_model_text(args["user"]) + "\n" + json.dumps(sanitize_model_value(args["public_payload"]),
        ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    wire, _estimate, ceiling = plan_request(chat_request_core(system=sanitize_model_text(args["system"]),
        user=user, schema=final, schema_hint={}, thinking_mode="disabled", effort=None, temperature=.2))
    assert hashlib.sha256(args["user"].encode()).hexdigest() == task_sha
    assert hashlib.sha256(canonical_json(wire).encode()).hexdigest() == wire_sha
    assert ceiling == MAX_OUTPUT_TOKENS
