"""MC-011 readiness and v4 completion contract."""
from __future__ import annotations

import pytest

from trading_system.agent_evidence import EvidenceInvalid
from trading_system.agent_runtime import (
    CURRENT_TECH_INTELLIGENCE_PROFILE, completion_status,
    policy_manifest,
    research_readiness,
)
from trading_system.redline.execution import CURRENT_TECH_INTELLIGENCE_POLICY_ID
from trading_system.research_work import research_work_policy


def set_model(monkeypatch):
    for name, value in {
        "LLM_BACKEND": "api",
        "LLM_BASE_URL": "https://api.deepseek.com",
        "LLM_MODEL": "deepseek-flash",
        "LLM_API_KEY": "fixture-key",
    }.items():
        monkeypatch.setenv(name, value)


def packages():
    return {name: "fixture-version" for name in (
        "pandas", "numpy", "yfinance", "requests", "PyYAML", "tokenizers",
    )}


def test_runtime_model_and_full_research_readiness_are_distinct(monkeypatch):
    set_model(monkeypatch)
    monkeypatch.setenv("TIGER_SEC_CONTACT", "operator@example.com")
    monkeypatch.setenv(
        "TIGER_INTEL_SEC_USER_AGENT",
        "TigerTest research operator@example.com",
    )
    full = research_readiness(packages())
    assert full["schema"] == "tiger.run-readiness/v1"
    assert full["runtime_ready"] and full["model_ready"]
    assert full["full_research_ready"] and not full["reduced_evidence"]
    monkeypatch.delenv("TIGER_SEC_CONTACT")
    monkeypatch.delenv("TIGER_INTEL_SEC_USER_AGENT")
    reduced = research_readiness(packages())
    assert reduced["runtime_ready"] and reduced["model_ready"]
    assert not reduced["full_research_ready"]
    assert reduced["reduced_evidence"]


def test_client_readiness_probe_is_boolean_only(monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("TIGER_SEC_CONTACT", raising=False)
    monkeypatch.delenv("TIGER_INTEL_SEC_USER_AGENT", raising=False)
    monkeypatch.setenv("TIGER_READINESS_INPUT", (
        '{"schema":"tiger.readiness-input/v1","api_key_present":true,'
        '"model_ready":true,"sec_contact":true,"sec_user_agent":true}'
    ))
    value = research_readiness(packages())
    assert value["model_ready"] and value["sec_identity_ready"]
    assert value["full_research_ready"] and not value["reduced_evidence"]


def test_v4_policy_freezes_readiness_and_forbids_buy_in_reduced_mode(monkeypatch):
    set_model(monkeypatch)
    monkeypatch.delenv("TIGER_SEC_CONTACT", raising=False)
    monkeypatch.delenv("TIGER_INTEL_SEC_USER_AGENT", raising=False)
    readiness = research_readiness(packages())
    policy = policy_manifest(
        CURRENT_TECH_INTELLIGENCE_PROFILE,
        readiness=readiness,
        research_work=research_work_policy(reliability=True),
        authoritative_paper=False,
    )
    assert policy["profile"] == CURRENT_TECH_INTELLIGENCE_PROFILE
    assert policy["execution"]["policy_id"] == CURRENT_TECH_INTELLIGENCE_POLICY_ID
    assert policy["execution"]["reduced_evidence"] is True
    assert policy["readiness"] == readiness
    assert policy["reliability_contract"] == "tiger.reliability-contract/v4"
    assert policy["research_work"]["task_quarantine_contract"] == "tiger.attempt-quarantine/v1"
    assert policy["research_work"]["progress_contract"] == "tiger.run-progress/v1"
    assert policy["research_work"]["decision_format_contract"] == "decision-format-v1"
    assert any("BUY is forbidden" in item for item in policy["limitations"])
    assert len(policy["stages"]) == 41


def test_v3_policy_bytes_do_not_gain_v4_readiness_fields(monkeypatch):
    set_model(monkeypatch)
    policy = policy_manifest("us-tech-intelligence-paper-v3")
    assert policy["profile"] == "us-tech-intelligence-paper-v3"
    assert "readiness" not in policy and "reliability_contract" not in policy
    assert "reduced_evidence" not in policy["execution"]


def test_readiness_rejects_unreviewed_shape():
    from trading_system.agent_runtime import validate_readiness

    with pytest.raises(EvidenceInvalid):
        validate_readiness({
            "schema": "tiger.run-readiness/v1",
            "runtime_ready": True,
            "model_ready": True,
            "sec_identity_ready": False,
            "full_research_ready": True,
            "reduced_evidence": True,
            "checks": {},
        })


def test_v4_rejects_completed_degraded_while_legacy_profile_remains_replayable():
    assert completion_status("us-tech-intelligence-paper-v3", True) == "completed_degraded"
    assert completion_status(CURRENT_TECH_INTELLIGENCE_PROFILE, False) == "completed"
    with pytest.raises(EvidenceInvalid, match="completed_degraded"):
        completion_status(CURRENT_TECH_INTELLIGENCE_PROFILE, True)


def test_doctor_ready_requires_model_and_sec_identity(monkeypatch, tmp_path):
    from trading_system import agent_runner

    set_model(monkeypatch)
    monkeypatch.setenv("TIGER_SEC_CONTACT", "operator@example.com")
    monkeypatch.setenv(
        "TIGER_INTEL_SEC_USER_AGENT",
        "TigerTest research operator@example.com",
    )
    value = agent_runner.doctor(tmp_path / "state", "readonly")
    assert value["status"] == "ready"
    assert value["data"]["runtime_ready"] is True
    assert value["data"]["model_ready"] is True
    assert value["data"]["full_research_ready"] is True
    monkeypatch.delenv("TIGER_INTEL_SEC_USER_AGENT")
    reduced = agent_runner.doctor(tmp_path / "state", "readonly")
    assert reduced["status"] == "reduced_evidence"
    assert reduced["data"]["readiness"]["reduced_evidence"] is True
