"""红线守卫（Redline Guard）— 系统执行层刚性约束的机器实现。

用户指令（红线原则）：
  1. 全链路严格执行：所有既定环节必须完整执行，严禁跳过，严禁擅自降级。
  2. LLM 驱动不可逆：定义为 LLM 驱动的环节，绝对禁止运行时回退为规则引擎。
  3. 异常处理：某环节无有效产出时，允许【透传兜底数据至下一环节】并记录日志；
     但绝不允许自行跳过环节，绝不允许把 LLM 环节改用规则执行。
  违反任一红线 = 系统性事故（RedlineViolation），须立即停止并复盘。

本模块把这三条落成代码事实：
  - STEP_REGISTRY：全链路环节注册表（含每环节的 driver 类型与透传兜底定义）；
  - ExecutionTracer：每次运行强制逐环节打点；运行结束时 assert_complete()
    校验注册环节无一遗漏，缺失即抛 RedlineViolation（立即停止）；
  - llm_guard()：LLM 环节的唯一合法降级路径——LLM 不可用/无产出时返回
    Passthrough（透传兜底对象）并写 WARNING 日志；代码库中不存在任何
    "LLM 失败 → 改用规则计算" 的分支，pytest 注入故障锁定该行为。
"""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable

log = logging.getLogger("redline")


class RedlineViolation(RuntimeError):
    """红线被违反 = 系统性事故。抛出即停止运行。"""


class LLMUnavailable(RuntimeError):
    """LLM 通道不可用（无 key / 无权限 / 网络失败 / 解析失败）。
    这是触发【透传兜底】的唯一信号，绝不触发规则回退。"""


@dataclass(frozen=True)
class StepSpec:
    """一个既定环节的定义。"""
    name: str               # 环节唯一名（注册表顺序即执行顺序）
    driver: str             # "rules" | "llm" | "hybrid"（hybrid=规则骨架+LLM 语义子环节）
    passthrough: str        # 无有效产出时的兜底描述（透传什么给下一环节）
    owner: str              # 责任模块


# 全链路环节注册表 —— 顺序即契约（与 pipeline.run_pipeline 的实际执行顺序一致）。
# 任何分析链路运行必须逐环节执行；新增环节只能在此注册，不允许代码里临时调用。
# 注：回测（backtest）是历史模拟，禁止注入当日搜索/LLM 信息（未来函数），
# 其环节契约由 backtest.py 自身的无未来函数测试锁定，不受本注册表约束。
STEP_REGISTRY: tuple[StepSpec, ...] = (
    StepSpec("search.collect",   "rules", "空文档集透传（源级失败不阻塞）",    "search.hub"),
    StepSpec("clean.rule_base",  "rules", "未去重原始文档透传",               "cleaning.pipeline"),
    StepSpec("clean.llm_semantic", "llm", "规则清洗后文档透传（无语义标注）",  "cleaning.pipeline"),
    StepSpec("clean.cross_validate", "rules", "全部文档按未验证透传披露（不阻塞管线）", "search.credibility"),
    StepSpec("data.prepare",     "rules", "（不可透传：行情数据是决策地基）",   "pipeline"),
    StepSpec("tech.monitor",     "rules", "行情动能空表透传",                 "tech_chain.agents"),
    StepSpec("tech.cycle_linkage", "rules", "全球联动空表透传",               "tech_chain.agents"),
    StepSpec("tech.sentiment",   "llm",   "文档透传（无情感分）",             "tech_chain.agents"),
    StepSpec("tech.risk",        "llm",   "文档透传（无风险预警）",           "tech_chain.agents"),
    StepSpec("tech.fusion",      "rules", "空 TechChainSignal 透传",          "tech_chain.fusion"),
    StepSpec("sector.narrative", "llm",   "无叙事评分透传（SHS该维度剔除再归一化）", "agents.narrative_agent"),
    StepSpec("layer1.mrs",       "rules", "（不可透传）",                     "agents.mrs_agent"),
    StepSpec("layer2.sector",    "hybrid","叙事维度缺失→剔除再归一化",         "agents.sector_agent"),
    StepSpec("layer2b.chain",    "rules", "ICS 缺失→无链加成",                "agents.chain_cycle_agent"),
    StepSpec("layer0.scan",      "rules", "（不可透传：无候选=当日AVOID）",    "agents.universe_scanner"),
    StepSpec("layer3.tss",       "hybrid","期权维度缺失→剔除再归一化",         "agents.tss_agent"),
    StepSpec("layer4.risk",      "rules", "（不可透传）",                     "agents.risk_manager_agent"),
    StepSpec("decision.debate",  "llm",   "无辩论证据透传（交易卡片第六段标注本轮无辩论）", "agents.debate_agent"),
    StepSpec("iteration.calibration", "rules", "（不可透传：失败则跳过披露并记录）", "calibration"),
    StepSpec("report.emit",      "rules", "（不可透传）",                     "report"),
    # S6 复盘闭环：复盘纪要在 pipeline 之外编排（依赖 daily 尾部的 journal
    # 落账/结算），属延迟环节——注册进全链路契约，pipeline 结束记账为
    # deferred，main.daily 尾部由 review.chief 实际执行后补记 executed。
    StepSpec("review.daily",     "rules", "纪要缺失→披露未生成原因",          "review.chief"),
)

LLM_STEPS = {s.name for s in STEP_REGISTRY if s.driver == "llm"}


def pipeline_steps(intelligence: bool = False) -> tuple[StepSpec, ...]:
    """Versioned order; historical profiles retain their exact original stages."""
    if not intelligence:
        return STEP_REGISTRY
    by_name = {step.name: step for step in STEP_REGISTRY}
    acquisition = (
        by_name["data.prepare"],
        StepSpec("intelligence.frontier", "rules", "研究对象或行情绑定缺失则失败", "intelligence_workflow"),
        *(by_name[name] for name in ("search.collect", "clean.rule_base", "clean.llm_semantic", "clean.cross_validate")),
        StepSpec("intelligence.freeze", "rules", "证据不足显式partial；篡改或并发封存失败", "intelligence_workflow"),
    )
    moved = {step.name for step in acquisition}
    return acquisition + tuple(step for step in STEP_REGISTRY if step.name not in moved)

# 延迟环节：由 pipeline 之外的编排层执行并补账（当前仅 review.daily）。
# assert_complete 不强制 pipeline 内打点；pipeline 末尾统一记 deferred 占位，
# 外层（main.daily）执行后把 redline 中该环节改写为 executed 留痕。
DEFERRED_STEPS = frozenset({"review.daily"})


@dataclass
class StepRecord:
    name: str
    status: str          # "executed" | "passthrough"
    elapsed_ms: float
    note: str = ""

    def output(self, value: Any) -> None:
        """Legacy tracing does not persist strict snapshots."""
        return None


@dataclass
class Passthrough:
    """透传兜底对象：环节无有效产出时，把上一环节的数据原样送往下一环节。
    `origin` 记录哪个环节触发了透传，`reason` 记录原因（LLM 不可用 / 无产出）。
    它的存在本身即红线 3 的审计证据。"""
    payload: Any
    origin: str
    reason: str
    degraded: bool = True

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Passthrough from={self.origin} reason={self.reason}>"


@dataclass
class ExecutionTracer:
    """一次完整运行的执行轨迹。环节通过 tracer.step(...) 打点；
    运行结束必须调用 assert_complete()，缺环节 = 系统性事故。"""
    run_id: str = ""
    records: list[StepRecord] = field(default_factory=list)
    execution: Any = None

    @contextmanager
    def step(self, name: str, inputs: Any = None):
        registry = pipeline_steps(bool(self.execution and getattr(self.execution, "intelligence", False)))
        spec = next((s for s in registry if s.name == name), None)
        if spec is None:
            raise RedlineViolation(f"未注册环节被调用: {name}（疑似擅自变更全链路）")
        if self.execution is not None:
            with self.execution.step(name, inputs) as scope:
                yield scope
            return
        t0 = time.perf_counter()
        rec = StepRecord(name=name, status="executed", elapsed_ms=0.0)
        self.records.append(rec)  # 进入即入账：llm_guard 的 mark_passthrough 才能标记到当前环节
        try:
            yield rec
        finally:
            rec.elapsed_ms = (time.perf_counter() - t0) * 1000.0

    def mark_passthrough(self, name: str, reason: str) -> None:
        if self.execution is not None:
            self.execution.mark_passthrough(name, reason)
            return
        log.warning("[红线·透传] 环节 %s 无有效产出，透传兜底至下一环节: %s", name, reason)
        for rec in reversed(self.records):
            if rec.name == name:
                rec.status = "passthrough"
                rec.note = reason
                return

    def assert_complete(self) -> None:
        if self.execution is not None:
            self.execution.assert_pipeline_complete()
            return
        executed = {r.name for r in self.records}
        missing = [s.name for s in STEP_REGISTRY
                   if s.name not in executed and s.name not in DEFERRED_STEPS]
        if missing:
            raise RedlineViolation(
                f"全链路环节缺失（红线 1 违反，系统性事故）: {missing}")

    def mark_deferred(self, name: str, note: str = "") -> None:
        """延迟环节占位记账（deferred）：pipeline 内不执行，由外层编排补账。"""
        if name not in DEFERRED_STEPS:
            raise RedlineViolation(f"非延迟环节不得记 deferred: {name}")
        self.records.append(StepRecord(
            name=name, status="deferred", elapsed_ms=0.0,
            note=note or "延迟至 pipeline 外编排层执行（执行后补记 executed）"))

    def summary(self) -> list[dict]:
        if self.execution is not None:
            names = {s.name for s in pipeline_steps(getattr(self.execution, "intelligence", False))} - DEFERRED_STEPS
            actual = [{"step": r["stage"],
                       "status": "executed" if r["status"] == "completed" else r["status"],
                       "ms": r["ms"], "note": r["reason"]}
                      for r in self.execution.summary() if r["stage"] in names]
            return actual + [{"step": r.name, "status": r.status,
                              "ms": round(r.elapsed_ms, 1), "note": r.note}
                             for r in self.records]
        return [{"step": r.name, "status": r.status,
                 "ms": round(r.elapsed_ms, 1), "note": r.note} for r in self.records]


def llm_guard(step_name: str, fn: Callable[[], Any], fallback_payload: Any,
              tracer: ExecutionTracer | None = None) -> Any:
    """LLM 环节的唯一合法执行方式。

    fn: 真正的 LLM 调用（内部抛 LLMUnavailable 表示不可用）。
    fallback_payload: 透传兜底数据（上一环节的原始输入，未经任何规则加工）。

    红线 2 的机器保证：本函数只有两个出口——
      (a) LLM 成功 → 返回 LLM 的真实产出；
      (b) LLM 不可用 → 返回 Passthrough(fallback_payload)，并写 WARNING 日志。
    不存在 (c) “用规则算一个结果冒充 LLM 产出”——调用方若需要那种行为，
    只能修改本函数签名，pytest 静态审查会锁定本文件。
    """
    if step_name not in LLM_STEPS:
        raise RedlineViolation(f"llm_guard 用于非 LLM 环节: {step_name}")
    required = bool(tracer is not None and tracer.execution is not None
                    and tracer.execution.model_required)
    try:
        out = fn()
    except LLMUnavailable as e:
        if required:
            from .execution import RequiredModelFailed
            raise RequiredModelFailed("Mandatory semantic model failed") from None
        if tracer is not None:
            tracer.mark_passthrough(step_name, f"LLM不可用: {e}")
        else:
            log.warning("[红线·透传] %s LLM不可用: %s", step_name, e)
        return Passthrough(payload=fallback_payload, origin=step_name,
                           reason=f"LLM不可用: {e}")
    if out is None:
        if required:
            from .execution import RequiredModelFailed
            raise RequiredModelFailed("Mandatory semantic model returned no result")
        if tracer is not None:
            tracer.mark_passthrough(step_name, "LLM无有效产出")
        return Passthrough(payload=fallback_payload, origin=step_name,
                           reason="LLM无有效产出")
    if required and isinstance(out, Passthrough):
        from .execution import RequiredModelFailed
        raise RequiredModelFailed("Mandatory semantic model cannot pass through")
    return out
