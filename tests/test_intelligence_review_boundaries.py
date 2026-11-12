"""Independent review regressions for admitted partial-research boundaries."""
from __future__ import annotations

import json
from dataclasses import asdict

import main
from trading_system.agent_evidence import verify_events
from trading_system.intelligence_workflow import stage_input_packet
from trading_system.llm.intelligence_proof import verify_intelligence_model
from trading_system.redline.execution import daily_stage_contract
from trading_system.report import render_markdown
from test_intelligence_workflow_v2 import intelligence_fixture


def test_no_registered_discovery_provider_keeps_partial_bundle_and_real_semantic_stages(tmp_path, monkeypatch):
    execution, root, args, _, denied, calls = intelligence_fixture(tmp_path, monkeypatch)
    path = tmp_path / "trusted-registry.json"
    config = json.loads(path.read_text())
    for source, settings in config["sources"].items():
        settings["enabled"] = source == "web_page"
    path.write_text(json.dumps(config))
    (root / "intelligence-input.json").unlink()
    stage_input_packet(root, as_of=execution.admitted_at)

    output = main._daily(args, "yahoo", execution=execution)

    execution.assert_complete()
    events = verify_events(root, execution.run_id, daily_stage_contract(True, intelligence=True))
    assert events["stage_count"] == 41 and not events["degraded"]
    intelligence = output.result.raw["intelligence"]
    assert intelligence["state"] == "partial" and intelligence["bundle"]["missing_required"]
    assert any(row["required"] and row.get("source_reason") == "NO_REGISTERED_DISCOVERY_PROVIDER"
               and any(item["reason"] == "NO_REGISTERED_DISCOVERY_PROVIDER"
                       for item in row["source_diagnostics"])
               for row in intelligence["gaps"])
    assert output.result.picks == [] and output.result.action in {"WAIT", "AVOID"}
    assert output.result.raw["model"]["search_count"] == 0
    assert {stage for stage, path in calls if path == "/chat/completions"} == {
        "clean.llm_semantic", "tech.sentiment", "tech.risk", "sector.narrative", "decision.debate"}
    assert not any(path != "/chat/completions" for _, path in calls)
    assert denied == []
    markdown = render_markdown(output.result)
    assert "本轮没有启用可用于补查的搜索工具" in markdown
    assert "本轮五个语义阶段均须取得完整有效回复" in markdown
    assert "缺失模型回复会阻止本轮完成" in markdown
    assert "LLM 不可用时按红线透传" not in markdown
    admission = {"clock": {"run_date": execution.run_date.isoformat(),
        "market_timezone": "America/New_York", "admitted_at_utc": execution.admitted_at.isoformat()}}
    assert verify_intelligence_model(root, asdict(output.result), admission) == output.result.raw["model"]
