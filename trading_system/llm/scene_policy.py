"""场景路由策略桥（v3.0 通用模型路由系统 · 交易内核侧）。

读取 governance/bundles/trading/model-policy.yml（bundle 第⑦装配槽），
为内核 LLM 环节提供统一的「场景 → 模型档 / 降级语义」口径：

  - 档位：L1 轻量 0.2× / L2 中坚 1× / L3 旗舰 3×（积分计量见底座 credits.ts）
  - 金融铁律：fallback=passthrough-disclose 的场景，LLM 失败唯一出路是
    透传披露（抛 LLMUnavailable），禁止降档重答——与红线 2 同义。

守卫：kernel_scenes() 列出内核全部 LLM 环节；validate_coverage() 校验
每个环节在策略表中有名（无名即配置事故，启动期暴露，不进运行期）。
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

import yaml

# 内核 LLM 环节 → 策略场景（与调用点一一对应，新增环节必须同步登记）
KERNEL_SCENE_MAP: dict[str, str] = {
    "clean.llm_semantic": "news-labeling",   # 新闻实体消歧/情感/事件抽取
    "tech.sentiment": "tech-sentiment",      # 科技子链舆情
    "tech.risk": "tech-risk",                # 供应链/政策/专利风险
    "sector.narrative": "sector-narrative",  # 板块叙事兑现分（进决策分）
    "decision.debate": "debate",             # 多空辩论（灰区标的）
}

_DEFAULT_POLICY_PATH = (
    Path(__file__).resolve().parents[2] / "governance" / "bundles" / "trading" / "model-policy.yml"
)


class ScenePolicyError(RuntimeError):
    """策略表缺失/非法/覆盖不全（启动期硬失败，不静默）。"""


@lru_cache(maxsize=1)
def load_policy(path: str | os.PathLike[str] | None = None) -> dict:
    """加载并校验 model-policy.yml；结果进程级缓存。"""
    p = Path(path) if path else Path(os.environ.get("TRADING_MODEL_POLICY", _DEFAULT_POLICY_PATH))
    if not p.exists():
        raise ScenePolicyError(f"模型路由策略缺失：{p}")
    doc = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    scenes = doc.get("scenes") or {}
    if not isinstance(scenes, dict):
        raise ScenePolicyError("model-policy.yml scenes 非法")
    return {"version": str(doc.get("version", "")), "scenes": scenes}


_THINKING_EFFORTS = frozenset({"low", "high", "max"})


def _thinking(raw: object, scene: str) -> dict:
    if raw is None or raw == "disabled":
        return {"type": "disabled"}
    if isinstance(raw, str) and raw in _THINKING_EFFORTS:
        return {"type": "enabled", "effort": raw}
    if isinstance(raw, dict):
        mode = raw.get("type")
        effort = raw.get("effort")
        if mode == "disabled" and set(raw) == {"type"}:
            return {"type": "disabled"}
        if (mode == "enabled" and set(raw) == {"type", "effort"}
                and effort in _THINKING_EFFORTS):
            return {"type": "enabled", "effort": effort}
    raise ScenePolicyError(f"模型路由场景 {scene} 的 thinking 配置非法")


def scene_config(scene: str, path: str | os.PathLike[str] | None = None) -> dict:
    """场景配置 {tier, fallback, noDowngrade, window, thinking}。"""
    scenes = load_policy(path)["scenes"]
    cfg = scenes.get(scene) or scenes.get("generic") or {"tier": "L2"}
    return {
        "tier": cfg.get("tier", "L2"),
        "fallback": cfg.get("fallback", "downgrade"),
        "noDowngrade": bool(cfg.get("noDowngrade", False)),
        "window": cfg.get("window", "any"),
        "thinking": _thinking(cfg.get("thinking", "disabled"), scene),
    }


def scene_tier(kernel_step: str, path: str | os.PathLike[str] | None = None) -> str:
    """内核环节 → 模型档（L1/L2/L3），计量与账单对齐底座口径。"""
    return scene_config(KERNEL_SCENE_MAP[kernel_step], path)["tier"]


def must_passthrough(kernel_step: str, path: str | os.PathLike[str] | None = None) -> bool:
    """该环节失败是否只允许透传披露（金融铁律；与 LLMUnavailable 语义对齐）。"""
    return scene_config(KERNEL_SCENE_MAP[kernel_step], path)["fallback"] == "passthrough-disclose"


def scene_thinking(kernel_step: str, path: str | os.PathLike[str] | None = None) -> dict:
    """Return the exact DeepSeek thinking mode and effort for a kernel stage."""
    if kernel_step not in KERNEL_SCENE_MAP:
        raise ScenePolicyError(f"内核模型环节未登记：{kernel_step}")
    return scene_config(KERNEL_SCENE_MAP[kernel_step], path)["thinking"]


def validate_coverage(path: str | os.PathLike[str] | None = None) -> list[str]:
    """覆盖守卫：返回未在策略表登记的内核环节清单（空 = 全覆盖）。"""
    scenes = load_policy(path)["scenes"]
    return [step for step, scene in KERNEL_SCENE_MAP.items() if scene not in scenes]
