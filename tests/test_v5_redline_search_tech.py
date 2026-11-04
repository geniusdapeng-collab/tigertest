"""v5.0 测试套件：红线约束 / 搜索集成 / 数据清洗 / 科技链子集群 / 全链路集成。

红线测试的核心：证明"LLM 失败 → 规则回退"这条路在架构上不存在——
注入永远失败的 LLM 后，系统输出只能是透传（degraded），不能出现任何
被规则"补算"出来的语义字段。
"""

from __future__ import annotations

import pytest

from trading_system.redline import (LLMUnavailable, Passthrough,
                                    RedlineViolation, ExecutionTracer,
                                    llm_guard, STEP_REGISTRY, LLM_STEPS)
from trading_system.search.models import RawDocument
from trading_system.search.hub import SearchHub
from trading_system.search.sources import DemoSearchSource
from trading_system.cleaning.pipeline import (rule_base_clean, llm_semantic_clean,
                                              unwrap_cleaned)
from trading_system.llm.prompts import CLEAN_SCHEMA, SENTIMENT_SCHEMA, RISK_SCHEMA


# ---------------------------------------------------------------- 假件
class FakeLLM:
    """永远成功的假 LLM（返回预设 JSON）。"""
    def __init__(self, out: dict):
        self.out = out
        self.calls = 0

    def complete_json(self, **kw):
        self.calls += 1
        return self.out


class BrokenLLM:
    """永远失败的 LLM（模拟无权限/无网络）。"""
    def complete_json(self, **kw):
        raise LLMUnavailable("测试注入: LLM 通道关闭")


def _doc(i: str, content: str, source: str = "demo", title: str = "t") -> RawDocument:
    return RawDocument(doc_id=i, source=source, title=title, url=f"u://{i}",
                       content=content, published="2026-07-30")


# ---------------------------------------------------------------- 红线
class TestRedline:
    def test_registry_llm_steps(self):
        assert {"clean.llm_semantic", "tech.sentiment", "tech.risk",
                "sector.narrative"} <= LLM_STEPS

    def test_llm_guard_success(self):
        out = llm_guard("tech.sentiment", lambda: {"x": 1}, fallback_payload=[])
        assert out == {"x": 1}

    def test_llm_guard_unavailable_returns_passthrough_not_rules(self):
        """红线 2 核心：LLM 失败 → Passthrough（原始 payload 原样透传）。"""
        docs = [_doc("a", "some content here long enough")]
        out = llm_guard("tech.sentiment",
                        lambda: (_ for _ in ()).throw(LLMUnavailable("off")),
                        fallback_payload=docs)
        assert isinstance(out, Passthrough)
        assert out.payload is docs          # 原样透传，零加工
        assert out.degraded is True

    def test_llm_guard_none_output_passthrough(self):
        out = llm_guard("tech.risk", lambda: None, fallback_payload=[1, 2])
        assert isinstance(out, Passthrough)

    def test_llm_guard_rejects_non_llm_step(self):
        with pytest.raises(RedlineViolation):
            llm_guard("layer1.mrs", lambda: 1, fallback_payload=None)

    def test_tracer_unregistered_step_raises(self):
        tr = ExecutionTracer()
        with pytest.raises(RedlineViolation):
            with tr.step("evil.skip"):
                pass

    def test_tracer_missing_step_is_systemic_accident(self):
        """红线 1：少执行一个注册环节 = 系统性事故。"""
        tr = ExecutionTracer()
        with tr.step("search.collect"):
            pass
        with pytest.raises(RedlineViolation, match="环节缺失"):
            tr.assert_complete()

    def test_tracer_passthrough_marked(self):
        tr = ExecutionTracer()
        with tr.step("clean.llm_semantic"):
            llm_guard("clean.llm_semantic",
                      lambda: (_ for _ in ()).throw(LLMUnavailable("off")),
                      fallback_payload=[], tracer=tr)
        rec = [r for r in tr.records if r.name == "clean.llm_semantic"][0]
        assert rec.status == "passthrough"
        assert "LLM不可用" in rec.note


# ---------------------------------------------------------------- 搜索
class TestSearch:
    def test_demo_source_deterministic(self):
        s = DemoSearchSource(seed=7)
        a = s.search("memory chips", 4)
        b = s.search("memory chips", 4)
        assert [d.doc_id for d in a] == [d.doc_id for d in b]
        assert len(a) == 4

    def test_hub_cache_hit(self):
        hub = SearchHub(sources=[DemoSearchSource()], use_disk_cache=False)
        b1 = hub.search("DRAM prices", 4)
        b2 = hub.search("DRAM prices", 4)
        assert "cache" in b2.source_stats          # 第二次零网络
        assert [d.doc_id for d in b1.docs] == [d.doc_id for d in b2.docs]

    def test_hub_source_failure_isolated(self):
        class BadSource:
            name = "bad"
            def search(self, q, limit=8):
                raise RuntimeError("boom")
        hub = SearchHub(sources=[DemoSearchSource(), BadSource()], use_disk_cache=False)
        batch = hub.search("AI capex", 4)
        assert batch.source_stats["bad"]["ok"] is False
        assert len(batch.docs) > 0                  # 好源不受影响

    def test_hub_circuit_breaker(self):
        class FlakySource:
            name = "flaky"
            def __init__(self): self.n = 0
            def search(self, q, limit=8):
                self.n += 1
                raise RuntimeError("x")
        src = FlakySource()
        hub = SearchHub(sources=[src, DemoSearchSource()], use_disk_cache=False)
        hub.ttl = 0
        for _ in range(3):
            hub.search("query" + str(src.n), 2)     # 每次新 query 绕开缓存
        n_before = src.n
        hub.search("another query", 2)
        assert src.n == n_before                    # 熔断后不再调用

    def test_query_plan_and_probes(self):
        qs = SearchHub.query_plan("semiconductors", ["NVDA", "AMD"])
        assert any("NVDA" in q for q in qs)
        probes = SearchHub.deep_probes("memory")
        assert any("patent" in p for p in probes)
        assert any("export control" in p for p in probes)


# ---------------------------------------------------------------- 清洗
class TestCleaning:
    def test_rule_base_dedup_and_format(self):
        d1 = _doc("a", "The quick brown fox jumps over the lazy dog. " * 5, title="Same Title")
        d2 = _doc("b", "The quick brown fox jumps over the lazy dog. " * 5, title="Same Title")
        d3 = _doc("c", "short")                       # 过短剔除
        d4 = _doc("d", "unique content about NVDA earnings and guidance. " * 3)
        out = rule_base_clean([d1, d2, d3, d4])
        ids = [d.doc_id for d in out]
        assert len(out) == 2                          # d2 指纹重复、d3 过短
        assert "c" not in ids

    def test_llm_semantic_applies_annotations(self):
        docs = [_doc("x1", "NVDA beat earnings and raised guidance strongly. " * 3)]
        llm = FakeLLM({"documents": [{
            "id": "x1", "tickers": ["NVDA"], "sentiment": "bullish",
            "sentiment_score": 0.9, "events": ["earnings", "guidance"],
            "summary_zh": "英伟达业绩超预期并上调指引", "relevance": 0.95}]})
        out = llm_semantic_clean(docs, llm)
        cleaned = unwrap_cleaned(out)
        assert cleaned[0].tickers == ["NVDA"]
        assert cleaned[0].sentiment == "bullish"
        assert cleaned[0].degraded is False

    def test_llm_broken_all_degraded_no_rule_backfill(self):
        """红线 2 行为证明：LLM 全灭时，输出只能全 degraded，
        且 sentiment/tickers 等语义字段必须为空（没有任何规则补算）。"""
        docs = [_doc("x1", "NVDA beat earnings strongly bullish amazing. " * 3)]
        out = llm_semantic_clean(docs, BrokenLLM())
        assert isinstance(out, Passthrough)
        cleaned = unwrap_cleaned(out)
        assert all(c.degraded for c in cleaned)
        assert all(c.sentiment is None and c.tickers == [] for c in cleaned)

    def test_cleaning_modules_have_no_sentiment_lexicon(self):
        """静态红线审查：清洗/科技链/LLM 模块源码禁止内置情感词典
        （情感分析若用规则词典实现即违反红线 2）。"""
        import inspect
        import trading_system.cleaning.pipeline as cp
        import trading_system.tech_chain.agents as ta
        banned = ["positive_words", "negative_words", "bullish_keywords",
                  "bearish_keywords", "SENTIMENT_LEXICON", "sentiment_lexicon"]
        for mod in (cp, ta):
            src = inspect.getsource(mod)
            for b in banned:
                assert b not in src, f"{mod.__name__} 出现规则情感词典 {b}"


# ---------------------------------------------------------------- 科技链
class TestTechChain:
    def test_monitor_six_chains(self):
        from trading_system.providers.demo import DemoProvider
        from trading_system.tech_chain.agents import TechChainMonitorAgent
        rows = TechChainMonitorAgent(DemoProvider()).execute()
        ids = {r.chain_id for r in rows}
        assert ids == {"memory", "logic", "foundry", "equipment", "ai_model", "ai_app"}
        assert any(r.chain_mom20 is not None for r in rows)

    def test_cycle_linkage_global_leaders(self):
        from trading_system.providers.demo import DemoProvider
        from trading_system.tech_chain.agents import CycleLinkageAgent
        rows = CycleLinkageAgent(DemoProvider()).execute()
        tickers = {r.ticker for r in rows}
        assert {"005930.KS", "000660.KS", "2330.TW", "TSM", "MU"} <= tickers

    def test_fusion_prosperity_and_risk_cap(self):
        from trading_system.tech_chain.agents import (ChainMonitorRow, LinkMomentum,
                                                      RiskAlert, SentimentRow)
        from trading_system.tech_chain.fusion import TechChainFusionAgent
        mon = [ChainMonitorRow(chain_id="memory", chain_mom20=0.06,
                               leading_link="midstream",
                               links=[LinkMomentum("upstream", 3, 0.08, 0.1, 0.8),
                                      LinkMomentum("midstream", 3, 0.04, 0.05, 0.6)])]
        sent = {"memory": SentimentRow(chain_id="memory", heat=8.0,
                                       sentiment_score=0.6,
                                       narrative_change="improving",
                                       degraded=False)}
        alerts = [RiskAlert(severity=9.0, chain_id="memory", link="midstream",
                            type="supply_chain", headline_zh="测试预警")]
        sigs = TechChainFusionAgent().execute(mon, [], sent, alerts)
        mem = next(s for s in sigs if s.chain_id == "memory")
        assert mem.prosperity is not None and 0 <= mem.prosperity <= 10
        assert mem.risk_level == 9.0
        assert mem.bonus_hint <= 1.0                    # 风险≥8 强制压制
        assert mem.transmission.get("upstream", {}).get("midstream") is not None
        # 全部维度在 → 无降级
        assert "tech.sentiment" not in mem.degraded_components

    def test_fusion_missing_risk_documents_does_not_gain_score(self):
        from trading_system.tech_chain.agents import ChainMonitorRow
        from trading_system.tech_chain.fusion import TechChainFusionAgent

        monitor = [ChainMonitorRow(chain_id="memory", chain_mom20=0.02)]
        fusion = TechChainFusionAgent()
        missing = fusion.execute(monitor, [], {}, [], risk_evidence_available=False)
        checked = fusion.execute(monitor, [], {}, [], risk_evidence_available=True)
        unknown_memory = next(row for row in missing if row.chain_id == "memory")
        checked_memory = next(row for row in checked if row.chain_id == "memory")
        assert unknown_memory.risk_level is None
        assert unknown_memory.prosperity == 6 and "tech.risk" in unknown_memory.degraded_components
        assert checked_memory.risk_level == 0 and checked_memory.prosperity > 6
        assert all(row.prosperity is None and row.bonus_hint == 1 for row in missing if row.chain_id != "memory")

    def test_fusion_degraded_passthrough(self):
        """LLM 环节透传时：fusion 剔除该维度再归一化，且如实披露降级。"""
        from trading_system.tech_chain.agents import ChainMonitorRow
        from trading_system.tech_chain.fusion import TechChainFusionAgent
        mon = [ChainMonitorRow(chain_id="memory", chain_mom20=0.05)]
        passthru = Passthrough(payload=[], origin="tech.sentiment", reason="LLM不可用")
        sigs = TechChainFusionAgent().execute(mon, [], passthru, passthru)
        mem = next(s for s in sigs if s.chain_id == "memory")
        assert "tech.sentiment" in mem.degraded_components
        assert "tech.risk" in mem.degraded_components
        assert mem.prosperity is not None              # 动能维度仍在 → 再归一化有分

    def test_bonus_map_picks_strongest(self):
        from trading_system.tech_chain.fusion import signals_to_bonus_map, TechChainSignal
        sigs = [TechChainSignal(chain_id="memory", main_chain="semis",
                                prosperity=8.0, risk_level=0.0, bonus_hint=1.10),
                TechChainSignal(chain_id="logic", main_chain="semis",
                                prosperity=4.0, risk_level=0.0, bonus_hint=0.95)]
        m = signals_to_bonus_map(sigs)
        assert m["semis"] == 1.10                       # 偏离 1.0 最远者优先


# ---------------------------------------------------------------- 全链路集成
class TestFullChainIntegration:
    def test_demo_pipeline_all_steps_executed(self):
        from trading_system.pipeline import run_pipeline
        r = run_pipeline(provider_name="demo", universe_mode="core", top_n=8, max_picks=3)
        redline = r.raw["redline"]
        executed = {s["step"] for s in redline}
        assert executed == {s.name for s in STEP_REGISTRY}     # 无一遗漏（红线 1）
        # LLM 环节：要么成功要么透传，绝不存在第三种状态
        for s in redline:
            if s["step"] in LLM_STEPS:
                assert s["status"] in ("executed", "passthrough")
        assert r.raw["tech_signals"]                            # 子集群信号已汇入
        assert r.action in ("BUY", "HOLD", "WAIT", "AVOID", "LIGHT")

    def test_demo_pipeline_repeatable(self):
        """demo 全链路确定性：两次运行科技链景气度一致。"""
        from trading_system.pipeline import run_pipeline
        r1 = run_pipeline(provider_name="demo", universe_mode="core", top_n=8, max_picks=3)
        r2 = run_pipeline(provider_name="demo", universe_mode="core", top_n=8, max_picks=3)
        p1 = [(s["chain_id"], s["prosperity"]) for s in r1.raw["tech_signals"]]
        p2 = [(s["chain_id"], s["prosperity"]) for s in r2.raw["tech_signals"]]
        assert p1 == p2
