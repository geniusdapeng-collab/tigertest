"""场景路由策略桥测试（v3.0）：策略加载 / 档位映射 / 金融降级语义 / 覆盖守卫。"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trading_system.llm.scene_policy import (  # noqa: E402
    KERNEL_SCENE_MAP,
    ScenePolicyError,
    load_policy,
    must_passthrough,
    scene_config,
    scene_thinking,
    scene_tier,
    validate_coverage,
)


def test_policy_loads_and_versions():
    doc = load_policy()
    assert doc["version"] == "v3.0"
    assert "debate" in doc["scenes"]


def test_kernel_steps_map_to_expected_tiers():
    assert scene_tier("clean.llm_semantic") == "L2"      # 新闻标注：中坚档谷时批量
    assert scene_tier("tech.sentiment") == "L2"
    assert scene_tier("tech.risk") == "L2"
    assert scene_tier("sector.narrative") == "L3"        # 进决策分：旗舰档
    assert scene_tier("decision.debate") == "L3"         # 多空辩论：旗舰档


def test_finance_passthrough_discipline():
    # 金融铁律：决策级语义失败只允许透传披露，禁止降档重答
    assert must_passthrough("decision.debate") is True
    assert must_passthrough("sector.narrative") is True
    # 标注类允许降级链（同档互备）
    assert must_passthrough("clean.llm_semantic") is False


def test_batch_scenes_in_offpeak_window():
    # 盘前批量天然谷时（×0.2 费率）
    assert scene_config("news-labeling")["window"] == "off-peak-only"
    assert scene_config("debate")["noDowngrade"] is True


def test_kernel_steps_use_scene_specific_thinking():
    assert scene_thinking("clean.llm_semantic") == {"type": "disabled"}
    assert scene_thinking("tech.sentiment") == {"type": "disabled"}
    assert scene_thinking("tech.risk") == {"type": "enabled", "effort": "high"}
    assert scene_thinking("sector.narrative") == {"type": "enabled", "effort": "high"}
    assert scene_thinking("decision.debate") == {"type": "enabled", "effort": "max"}


def test_coverage_guard_full():
    # 内核全部 LLM 环节必须在策略表登记（无名即配置事故）
    assert validate_coverage() == []
    assert set(KERNEL_SCENE_MAP) == {
        "clean.llm_semantic", "tech.sentiment", "tech.risk",
        "sector.narrative", "decision.debate",
    }


def test_missing_policy_hard_fails(tmp_path):
    with pytest.raises(ScenePolicyError):
        load_policy(tmp_path / "not-exists.yml")
