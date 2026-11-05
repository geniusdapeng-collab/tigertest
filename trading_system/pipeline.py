"""流水线引擎 — v5.0 全链路（搜索→清洗→科技链子集群→六层决策），
结构化上下文传递（禁止字符串解析），全环节红线打点（严禁跳过/降级）。

执行顺序（与 redline.STEP_REGISTRY 一致）：
  search.collect → clean.rule_base → clean.llm_semantic
  → tech.monitor / cycle_linkage / sentiment / risk / fusion（科技股专项子集群）
  → sector.narrative（SHS 叙事 LLM 化）
  → MRS(L1) → SHS(L2) → ICS(L2.5) → 全市场扫描(L0) → TSS(L3) → 风控(L4)
  → report.emit（结果封装）
"""

from __future__ import annotations

import logging
import time
from datetime import datetime

import pandas as pd

from . import config
from .agents import (
    ChainCycleAgent, MRSAgent, RiskManagerAgent, SectorAgent, TSSAgent,
    UniverseScannerAgent,
)
from .agents.narrative_agent import NarrativeAgent
from .cleaning.pipeline import llm_semantic_clean, rule_base_clean, unwrap_cleaned
from .data_models import PipelineResult
from .events import EventCalendar
from .llm.client import DisabledLLMClient, default_client
from .market_close import (close_chart, close_metadata, close_observation,
                           macro_limits, requires_foreign_calendar)
from .providers import get_provider
from .providers.base import is_synthetic
from .redline import ExecutionTracer, Passthrough
from .redline.execution import (EvidenceWriteError, ExecutionContractError,
                                StrictDataUnavailable, StrictDataStale, StrictObservationError,
                                optional_step, record_provider)
from .search.hub import SearchHub
from .tech_chain.agents import (
    ChainRiskAgent, ChainSentimentAgent, CycleLinkageAgent, TechChainMonitorAgent,
)
from .tech_chain.fusion import TechChainFusionAgent, signals_to_bonus_map
from .tech_chain.risk_evidence import risk_coverage
from .tech_chain.universe import CHAIN_TOPICS, SECTOR_TOPICS, TECH_SUBCHAINS
from .universe import load_universe

logger = logging.getLogger(__name__)


_LINEAGE: list[tuple[str, str]] = []   # 本轮行情来源血缘（对象, 实际源），每轮清空
# v6.0 provider 健康度（白皮书§12 披露文化的延伸）：本轮内按源记录调用
# 成功/失败/时延；连续失败 ≥2 次的源在本轮降级顺序中沉底；摘要写入报告。
_HEALTH: dict[str, dict] = {}
# 回退失败与最终缺失的结构化披露；只记异常类型，避免把源端 URL/凭据写入报告。
_DATA_ISSUES: list[dict] = []


class ProviderDataError(RuntimeError):
    """回退链耗尽的安全诊断，不保留源端异常正文或异常对象。"""

    def __init__(self, provider: str, method: str, error_type: str):
        self.provider = provider
        self.method = method
        self.error_type = error_type
        super().__init__(f"{provider}.{method} 数据获取失败（{error_type}）")


def _provider_source(provider) -> str:
    """来源标签必须遵守 synthetic 标记，合成源改名也不能伪装成真实源。"""
    return "demo" if is_synthetic(provider) else provider.name


def _data_issue(provider, method: str, symbols: list[str], issue: str,
                *, error: Exception | None = None, label: str | None = None) -> None:
    item = {"provider": provider.name, "synthetic": is_synthetic(provider),
            "method": method, "symbols": list(symbols), "issue": issue}
    if error is not None:
        item["error_type"] = type(error).__name__
    if label is not None:
        item["label"] = label
    _DATA_ISSUES.append(item)


def _missing_data(out) -> bool:
    """空容器/全 NaN 不是成功取数；有效数据保持原对象与原值。"""
    if out is None:
        return True
    if isinstance(out, (pd.DataFrame, pd.Series)):
        return out.empty or out.dropna(how="all").empty
    return False


def _health_record(name: str, ok: bool, elapsed: float) -> None:
    h = _HEALTH.setdefault(name, {"ok": 0, "fail": 0, "secs": 0.0})
    h["ok" if ok else "fail"] += 1
    h["secs"] = round(h["secs"] + elapsed, 2)


def _health_score(name: str) -> float:
    """健康分：成功率优先，连续失败惩罚（用于降级顺序自适应排序）。"""
    h = _HEALTH.get(name)
    if not h:
        return 0.5
    total = h["ok"] + h["fail"]
    return (h["ok"] + 1) / (total + 2) - min(h["fail"], 4) * 0.1


def _channel_chain(execution=None) -> list:
    """降级通道群（v6.2 七环制 + v3 官方宏观源），按本轮健康分自适应排序：
    official(FRED/CBOE 官方宏观基准：利率/波动率)
    → tencent(腾讯) / sina(新浪) / eastmoney(东财) 中国免费源（无需 key）
    → agentgw(yahoo_finance 服务端) → ifind_gw(同花顺服务端) → tiingo(key)。
    完整降级链：yahoo → stooq → 本通道群。"""
    chain: list = []
    for cls_path in (("official", "OfficialMacroProvider"),
                     ("tencent", "TencentProvider"),
                     ("sina", "SinaProvider"),
                     ("eastmoney", "EastMoneyProvider"),
                     ("agentgw", "AgentGwProvider"),
                     ("ifind_gw", "IfindGwProvider"),
                     ("tiingo", "TiingoProvider")):
        if execution is not None and cls_path[0] not in execution.policy.provider_names:
            continue
        try:
            mod = __import__(f"trading_system.providers.{cls_path[0]}",
                             fromlist=[cls_path[1]])
            cls = getattr(mod, cls_path[1])
            available = (execution.call_io(
                f"provider.{cls_path[0]}.available", {"provider": cls_path[0]}, cls.available)
                if execution is not None else cls.available())
            if available:
                chain.append(record_provider(cls(), execution))
        except (EvidenceWriteError, ExecutionContractError, StrictObservationError):
            raise
        except Exception:
            continue
    chain.sort(key=lambda p: _health_score(p.name), reverse=True)
    return chain


def _series_stale(out, max_lag_days: int = 5, *, as_of_date=None) -> bool:
    """宏观序列陈旧判定（v3 修复：tencent VIX 曾返回 4 个月前的陈旧序列）。
    仅对带 DatetimeIndex 的 pd.Series 生效；末根距今 >max_lag_days 个自然日
    视为陈旧（容忍官方源自然滞后：FRED ~1 个交易日、周末/假期顺延，
    5 个自然日足以覆盖，远小于"陈旧数月"的故障模式）。"""
    try:
        import pandas as pd
        if not isinstance(out, pd.Series) or out.empty:
            return False
        idx = out.index
        if not isinstance(idx, pd.DatetimeIndex):
            return False
        last = idx.max().to_pydatetime().date()
        return ((as_of_date or datetime.now().date()) - last).days > max_lag_days
    except Exception:
        return False


def _supports(provider, method: str) -> bool:
    """能力感知（v3 修复）：provider 声明 CAPABILITIES 时，不含该方法直接跳过，
    避免"必然失败的调用"污染健康分与降级时延（如 official 不承接个股 OHLCV）。"""
    caps = getattr(provider, "CAPABILITIES", None)
    if caps is None:
        # 未声明时按基类接口推断：rate_yield_for/vol_index_for 等非 OHLCV 方法
        # 对所有 provider 视为支持（基类有默认实现）；未声明者不做裁剪。
        return True
    if method == "ohlcv_batch":
        # 基类批量方法逐只委托 ohlcv；仅声明 ohlcv 的源仍可用批量接口。
        return "ohlcv_batch" in caps or "ohlcv" in caps
    return method in caps


def _single_with_fallback(provider, method: str, *args, close_cutoff=None, **kwargs):
    """单只/单序列拉取：yahoo → stooq → 服务端通道群（agentgw/ifind/tiingo）。

    v3 修复两条降级协议缺陷（2026-08-30 真实运行暴露）：
      1) None/空表/全 NaN 视同失败，继续寻找后续真实可用源；
      2) 序列级陈旧检测：返回的宏观序列末根过旧时视同失败继续降级。
    """
    execution = getattr(provider, "execution", None)
    as_of = close_cutoff or (execution.run_date if execution is not None else None)
    prior_close_observed = False

    def fetch(src):
        nonlocal prior_close_observed
        observed = getattr(src, method)(*args, **kwargs)
        if close_cutoff is None or _missing_data(observed):
            return observed
        exact = method == "ohlcv"
        bounded = close_observation(observed, close_cutoff, exact=exact)
        if bounded is None and exact and isinstance(observed, pd.DataFrame):
            prior_close_observed |= bool(len(observed.loc[observed.index.date < close_cutoff]))
        return bounded

    what = f"{method}({args[0] if args else ''})"
    t0 = time.time()
    try:
        out = fetch(provider)
        if _missing_data(out):
            kind = "None" if out is None else "空数据（含全 NaN）"
            raise RuntimeError(f"{provider.name}.{method} 返回 {kind}（视同失败，继续降级）")
        if _series_stale(out, as_of_date=as_of):
            raise RuntimeError(f"{provider.name}.{method} 序列陈旧（末根滞后超限，视同失败）")
        _LINEAGE.append((what, _provider_source(provider)))
        _health_record(provider.name, True, time.time() - t0)
        return out
    except (EvidenceWriteError, ExecutionContractError, StrictObservationError):
        raise
    except Exception as e:
        _health_record(provider.name, False, time.time() - t0)
        _data_issue(provider, method, list(args[:1]), "provider_error", error=e)
        failure = ProviderDataError(provider.name, method, type(e).__name__)
        chain: list = []
        if provider.name == "yahoo" and (execution is None or "stooq" in execution.policy.provider_names):
            from .providers.stooq import StooqProvider
            chain.append(record_provider(StooqProvider(allow_challenge=False), execution) if execution else StooqProvider())
        chain.extend(p for p in (_channel_chain(execution) if execution else _channel_chain())
                     if p.name != provider.name and _supports(p, method))
        for alt in chain:
            if not is_synthetic(provider) and is_synthetic(alt):
                _data_issue(alt, method, list(args[:1]), "synthetic_fallback_rejected")
                logger.warning("%s.%s 是合成源，真实数据回退链跳过该源", alt.name, method)
                continue
            t1 = time.time()
            try:
                logger.warning("%s.%s 失败（%s），降级 %s",
                               failure.provider, method, failure.error_type, alt.name)
                out = fetch(alt)
                if _missing_data(out):
                    kind = "None" if out is None else "空数据（含全 NaN）"
                    raise RuntimeError(f"{alt.name}.{method} 返回 {kind}（视同失败，继续降级）")
                if _series_stale(out, as_of_date=as_of):
                    raise RuntimeError(f"{alt.name}.{method} 序列陈旧（末根滞后超限，视同失败）")
                _LINEAGE.append((what, _provider_source(alt)))
                _health_record(alt.name, True, time.time() - t1)
                return out
            except (EvidenceWriteError, ExecutionContractError, StrictObservationError):
                raise
            except Exception as e2:
                _health_record(alt.name, False, time.time() - t1)
                _data_issue(alt, method, list(args[:1]), "provider_error", error=e2)
                failure = ProviderDataError(alt.name, method, type(e2).__name__)
    # 离开原异常处理块再抛出，避免 __context__ / traceback 带出源端正文。
    if prior_close_observed:
        raise StrictDataStale(f"{method} 缺少已完成交易日 {close_cutoff} 的可用收盘数据") from None
    raise failure from None


def _quote_stale(q: dict, max_lag_days: int = 3) -> bool:
    """所有报价统一校验时间，realtime 标签不能证明数据新鲜。

    保留距最近交易日 >max_lag_days 个自然日的容忍口径；源已标记陈旧、
    时间缺失/无效或晚于当前时刻时同样弃用，不能把未知时间当作当前价。
    """
    from datetime import date

    try:
        from .calendar import prev_trading_day
        if q.get("stale"):
            return True
        ts = q.get("ts")
        if not isinstance(ts, (str, date)):
            return True
        # 校验完整时间，不能截掉无效后缀；支持源端日期、空格/ISO 时间与时区。
        quoted_at = datetime.fromisoformat(str(ts).strip().replace("Z", "+00:00"))
        now = datetime.now(quoted_at.tzinfo)
        return (quoted_at > now
                or (prev_trading_day(datetime.now().date())
                    - quoted_at.date()).days > max_lag_days)
    except (TypeError, ValueError, OverflowError):
        return True


def quote_with_fallback(provider, ticker: str) -> dict | None:
    """当前报价降级链：provider → stooq → 服务端通道群（agentgw/ifind/tiingo）。

    与 _single_with_fallback 的区别：quote 失败时各 provider 返回 None 而非
    抛异常，旧链路会把 None 当成功、静默拿不到价（盘中触发器因此零警报）。
    本函数把 None 视为失败继续降级，并把报价来源与时效（kind:
    realtime / realtime_delayed / eod_close）记入来源血缘如实披露。
    报价必须是有限正数且时间可验证；任意 kind 落后最近交易日 >3 天、
    源标记陈旧、时间未知/无效/未来时继续降级。全链无有效报价时保留首个
    价格有效但时间不可用的报价并标注 stale=True（触发器将弃用，
    用上周的价格触发止损/入场比不报警更危险）。
    """
    import math
    from numbers import Real
    execution = getattr(provider, "execution", None)

    def sources():
        # 正常主源成功后不探测无关通道；实际回退顺序保持不变。
        yield provider
        if provider.name != "stooq" and (execution is None or "stooq" in execution.policy.provider_names):
            try:
                from .providers.stooq import StooqProvider
                yield record_provider(StooqProvider(allow_challenge=False), execution) if execution else StooqProvider()
            except (EvidenceWriteError, ExecutionContractError, StrictObservationError):
                raise
            except Exception as exc:
                logger.warning("stooq 报价源初始化失败（%s），尝试下一环",
                               type(exc).__name__)
        yield from (p for p in (_channel_chain(execution) if execution else _channel_chain())
                    if p.name != provider.name)

    stale_q: dict | None = None
    for src in sources():
        if src is not provider and not is_synthetic(provider) and is_synthetic(src):
            _data_issue(src, "quote", [ticker], "synthetic_fallback_rejected")
            logger.warning("%s.quote 是合成源，真实报价回退链跳过该源", src.name)
            continue
        try:
            q = src.quote(ticker)
        except (EvidenceWriteError, ExecutionContractError, StrictObservationError):
            raise
        except Exception as e:
            _data_issue(src, "quote", [ticker], "provider_error", error=e)
            logger.warning("%s.quote(%s) 异常（%s），降级下一环",
                           src.name, ticker, type(e).__name__)
            continue
        if not isinstance(q, dict) or not q:
            _data_issue(src, "quote", [ticker], "no_data" if q is None else "invalid_data")
            logger.warning("%s.quote(%s) 无数据，降级下一环", src.name, ticker)
            continue
        price = q.get("price")
        try:
            valid_price = (not isinstance(price, bool) and isinstance(price, Real)
                           and math.isfinite(price) and price > 0)
        except (TypeError, ValueError, OverflowError):
            valid_price = False
        if not valid_price:
            _data_issue(src, "quote", [ticker], "invalid_price")
            logger.warning("%s.quote(%s) 价格无效，降级下一环", src.name, ticker)
            continue
        if _quote_stale(q):
            _data_issue(src, "quote", [ticker], "stale_or_unverifiable_quote")
            logger.warning("%s.quote(%s) 报价陈旧或时间不可验证，尝试下一环",
                           src.name, ticker)
            stale_q = stale_q or {**q, "stale": True}
            continue
        _LINEAGE.append((f"quote({ticker})[{q.get('kind', '?')}]", _provider_source(src)))
        return q
    if stale_q is not None:
        _LINEAGE.append((f"quote({ticker})[stale]", "STALE"))
        return stale_q
    _data_issue(provider, "quote", [ticker], "quote_unavailable")
    return None


def _batch_with_fallback(provider, tickers: list[str], days: int,
                         label: str = "batch", *, close_cutoff=None, minimum=None) -> dict[str, pd.DataFrame]:
    """批量拉取：yahoo → stooq → 服务端通道群依次补洞
    （仅真实源之间降级；生产链路绝不回退合成数据，覆盖率如实记录 data_coverage）。"""
    execution = getattr(provider, "execution", None)
    if not tickers:
        return {}

    def _fetch(src, missing: list[str]) -> dict[str, pd.DataFrame]:
        if src is not provider and not is_synthetic(provider) and is_synthetic(src):
            _data_issue(src, "ohlcv_batch", missing, "synthetic_fallback_rejected", label=label)
            logger.warning("%s 是合成源，真实数据回退链跳过该源", src.name)
            return {}
        if not _supports(src, "ohlcv_batch"):
            _data_issue(src, "ohlcv_batch", missing, "unsupported", label=label)
            return {}
        try:
            batch = src.ohlcv_batch(missing, days=days)
        except (EvidenceWriteError, ExecutionContractError, StrictObservationError):
            raise
        except Exception as exc:
            _data_issue(src, "ohlcv_batch", missing, "provider_error", error=exc, label=label)
            logger.warning("%s.ohlcv_batch 批量拉取异常（%s），跳过该环继续降级",
                           src.name, type(exc).__name__)
            return {}
        if not isinstance(batch, dict):
            _data_issue(src, "ohlcv_batch", missing, "invalid_data", label=label)
            logger.warning("%s 批量结果不是行情字典，跳过该环继续降级", src.name)
            return {}
        got = {t: batch[t] for t in missing if t in batch
               and isinstance(batch[t], pd.DataFrame) and not _missing_data(batch[t])}
        invalid = [t for t in missing if t in batch and t not in got]
        if invalid:
            _data_issue(src, "ohlcv_batch", invalid, "invalid_data", label=label)
        if close_cutoff is not None:
            aligned = {symbol: close_observation(frame, close_cutoff, exact=True)
                       for symbol, frame in got.items()}
            absent = [symbol for symbol, frame in aligned.items() if frame is None]
            if absent:
                _data_issue(src, "ohlcv_batch", absent, "completed_session_bar_missing", label=label)
            got = {symbol: frame for symbol, frame in aligned.items() if frame is not None}
        if not got:
            _data_issue(src, "ohlcv_batch", missing, "no_data", label=label)
        availability = getattr(src, "last_batch_status", None)
        if (src.name == "stooq" and isinstance(availability, dict)
                and availability.get("source_exhausted") is True):
            _data_issue(src, "ohlcv_batch", availability["unattempted_symbols"],
                        "source_exhausted", label=label)
            _DATA_ISSUES[-1]["availability"] = dict(availability)
        return got

    def _finish() -> dict[str, pd.DataFrame]:
        missing = [t for t in tickers if t not in data]
        if missing:
            _data_issue(provider, "ohlcv_batch", missing,
                        "batch_missing" if data else "batch_unavailable", label=label)
            logger.warning("%s 批量数据缺失（%d/%d），缺失标的已记录 data_issues",
                           label, len(data), len(tickers))
        return data

    data = _fetch(provider, tickers)
    enough = minimum if minimum is not None else max(3, len(tickers) // 3)
    _LINEAGE.append((f"{label}[{len(data)}/{len(tickers)}]", _provider_source(provider)))
    if len(data) >= enough:
        return _finish()
    if provider.name != "stooq" and (execution is None or "stooq" in execution.policy.provider_names):
        logger.warning("%s 批量拉取覆盖率不足（%d/%d），降级 stooq",
                       provider.name, len(data), len(tickers))
        from .providers.stooq import StooqProvider
        missing = [t for t in tickers if t not in data]
        stooq = (record_provider(StooqProvider(allow_challenge=False,
                     max_consecutive_no_output=3), execution) if execution else StooqProvider())
        got = _fetch(stooq, missing) if missing else {}
        data.update(got)
        if got:
            _LINEAGE.append((f"{label}+补{len(got)}只", _provider_source(stooq)))
        if len(data) >= enough:
            return _finish()
    for alt in (_channel_chain(execution) if execution else _channel_chain()):
        missing = [t for t in tickers if t not in data]
        if not missing or len(data) >= len(tickers):
            break
        logger.warning("覆盖率仍不足（%d/%d），%s 通道补 %d 只",
                       len(data), len(tickers), alt.name, len(missing))
        got = _fetch(alt, missing)
        data.update(got)
        if got:
            _LINEAGE.append((f"{label}+补{len(got)}只", _provider_source(alt)))
    return _finish()


def run_pipeline(
    provider_name: str | None = None,
    universe_mode: str = "extended",
    universe_file: str | None = None,
    top_n: int = config.SCAN_TOP_N,
    max_picks: int = config.MAX_PICKS_DEFAULT,
    account_usd: float = 100_000,
    trade_date: str | None = None,
    use_tuned: bool = False,
    market: str = "us",
    accounting_blocked_reason: str | None = None,
    portfolio_state: dict | None = None,
    *, execution=None,
) -> PipelineResult:
    with optional_step(execution, "pipeline.prepare", {
        "provider": provider_name, "universe_mode": universe_mode,
        "universe_file": universe_file, "top_n": top_n, "max_picks": max_picks,
        "account_usd": account_usd, "market": market, "use_tuned": use_tuned,
        "run_date": execution.run_date if execution else trade_date,
        "policy": execution.policy if execution else None,
        "portfolio_state": portfolio_state,
    }) as prepared:
        started = time.time()
        _LINEAGE.clear()   # 零基线：来源血缘每轮重新记录
        _HEALTH.clear()    # 零基线：provider 健康度每轮重新度量
        _DATA_ISSUES.clear()
        if execution is not None:
            if trade_date is not None and trade_date != execution.run_date.isoformat():
                raise ExecutionContractError("Trade date differs from admission-market date")
            if market != "us" or use_tuned:
                raise ExecutionContractError("Strict public paper requires US market and unchanged defaults")
            trade_date = execution.run_date.isoformat()
        else:
            trade_date = trade_date or datetime.now().strftime("%Y-%m-%d")
        provider = (get_provider(provider_name, execution=execution) if execution
                    else get_provider(provider_name))
        # S4 多市场（D1 框架不变、输入替换）：市场规格解析——日历/基准组/板块代理/
        # 扫描过滤/合规规则/轻仓毕业门槛全部来自 spec（参数 single source = config）。
        from .markets import get_market
        spec = get_market(market)
        bmk = spec.benchmarks
        benchmark_labels = {"index": bmk.get("index_label", bmk["index"]),
                            "rate": bmk.get("rate_label", bmk["rate"]),
                            "vol": bmk.get("vol_label", bmk["vol"])}
        ah_enabled = bool(config.AH_TOPICS_ENABLED
                          or spec.market_id in config.AH_AUTO_MARKETS)
        market_grad = spec.graduation()
        universe_source = universe_mode
        if spec.market_id == "us":
            if universe_mode == "full":
                from .universe import load_full_universe
                universe, universe_source = load_full_universe()   # nasdaqtrader/cache/fallback
            else:
                universe = load_universe(universe_mode, universe_file)
        else:
            # CN/HK：full=东财全量清单（两级拉取复刻 US）；其他=内嵌池/文件
            from .universe import load_market_universe
            universe, universe_source = load_market_universe(
                spec.market_id, universe_mode, universe_file)
        tracer = ExecutionTracer(run_id=trade_date, execution=execution)
        demo_mode = is_synthetic(provider)

        # ================= 搜索与清洗（系统的命脉环节） =================
        required = execution is not None and execution.model_required
        security_roster = None
        if required and execution.completed_session_close is not None:
            from .security_roster import observed_roster
            security_roster = observed_roster(
                universe, execution.completed_session_close.date(), execution.admitted_at)
        if required:
            from .llm.deepseek_required import DeepSeekRequiredClient
            from .llm import required as semantic
            if getattr(execution, "intelligence", False):
                from .llm import intelligence_required as semantic
            try:
                llm = DeepSeekRequiredClient(execution)
                assessment = semantic.assessment_context(execution)
            except Exception as exc:
                from .redline import LLMUnavailable
                from .redline.execution import RequiredModelFailed
                if isinstance(exc, LLMUnavailable):
                    raise RequiredModelFailed("Required DeepSeek startup configuration is invalid") from None
                raise
        else:
            llm = DisabledLLMClient(execution) if execution is not None else default_client()
        event_calendar = (EventCalendar(str(execution.paths.calendar_override_path), strict=True)
                          if execution is not None else None)
        options_store = None
        if execution is not None:
            from .options_metrics import OptionsHistoryStore
            options_store = OptionsHistoryStore(execution.paths.options_history_dir,
                                                strict=True, as_of_date=execution.run_date,
                                                execution=execution)
        prepared.output({"trade_date": trade_date, "provider": provider.name,
                         "synthetic": demo_mode, "market": spec.to_dict(),
                         "universe": universe, "universe_source": universe_source,
                         **({"security_roster": security_roster} if security_roster else {}),
                         "benchmark_labels": benchmark_labels, "scan_filters": spec.scan_filters(),
                         "market_grad": market_grad, "ah_topics_enabled": ah_enabled,
                         "calendar": event_calendar.to_dict() if event_calendar else None,
                         "model": llm.describe() if execution is not None else {"mode": "legacy"}})

    def prepare_market(universe):
        signal_close = execution.completed_session_close if required else None
        session = signal_close.date() if signal_close is not None else None
        macro_dates = macro_limits(signal_close, execution.admitted_at) if signal_close else {}
        close_binding = close_metadata(signal_close) if signal_close else None

        # ---------- 数据真实性与时效性闸门（v6.1，白皮书§12"地基不牢评分皆是沙上之塔"）----------
        # S4：日历/指数名/板块清单全部按市场规格参数化（US 行为与现状完全一致）

        def _freshness_gate(spy_df, tnx_s, vix_s, etfs: dict, stocks: dict) -> dict:
            """硬依赖充足性 + 末根日期新鲜度 + 陈旧标的剔除。

            - 指数行数不足 → 诚实失败（短数据照样能"算出分"，但口径全错）；
            - 指数末根日期落后最近交易日 >3 天 → 诚实失败（基准数据陈旧）；
            - 利率/波动率历史不足：US → 诚实失败（现状行为）；CN/HK → 记缺失
              走"剔除再归一化"（D2，不可得维度不编造）；
            - 板块覆盖率 < 2/3：US → 诚实失败；CN/HK → 按缺失披露继续（D2）；
            - 个股末根日期落后指数末根 >5 天 → 判定停牌/源不完整，【剔除】并披露
              （绝不把上一周的价格当"当前价格"参与评分）。
            """
            report: dict = {}
            idx_name = bmk["index"]
            if len(spy_df) < 260:
                raise (StrictDataUnavailable if execution else RuntimeError)(
                    f"[{spec.market_id}] {idx_name} 历史不足（{len(spy_df)} < 260 行）："
                    "SMA200/252 日分位口径不成立，"
                    "宁可中止也不产出带污点的评分（白皮书§1.3 诚实失败）")
            tnx_ok = tnx_s is not None and len(tnx_s) >= 100
            vix_ok = vix_s is not None and len(vix_s) >= 30
            if spec.benchmark_hard_fail:
                if not tnx_ok or not vix_ok:
                    raise (StrictDataUnavailable if execution else RuntimeError)(
                        f"[{spec.market_id}] {bmk['rate']}/{bmk['vol']} 历史不足"
                        f"（{0 if tnx_s is None else len(tnx_s)}/"
                        f"{0 if vix_s is None else len(vix_s)} 行），MRS 宏观/情绪维口径不成立")
            else:
                missing_bmk = [lbl for lbl, ok in ((bmk["rate"], tnx_ok), (bmk["vol"], vix_ok))
                               if not ok]
                if missing_bmk:
                    report["benchmark_missing"] = missing_bmk   # 缺失维披露（再归一化）
            expect = session or spec.prev_trading_day(trade_date)
            spy_last = spy_df.index[-1].date()
            lag = (expect - spy_last).days
            report["benchmark_last_bar"] = str(spy_last)
            report["benchmark_lag_days"] = lag
            if execution is not None and lag < 0:
                raise StrictObservationError("Benchmark is later than the expected market trading date")
            if lag > 3:
                raise (StrictDataStale if execution else RuntimeError)(
                    f"[{spec.market_id}] 基准数据陈旧：{idx_name} 末根 {spy_last}，"
                    f"最近交易日 {expect}（滞后 {lag} 天）——"
                    "用陈旧数据产出'今日'评分是数据造假，立即中止（白皮书§12.3）")
            if lag >= 1:
                logger.warning("数据时效披露：%s 末根 %s（最近交易日 %s，滞后 %d 天）",
                               idx_name, spy_last, expect, lag)
            sector_list = spec.sector_symbols
            need_etf = (len(sector_list) * 2 + 2) // 3     # ≥2/3
            if len(etfs) < need_etf:
                missing = [e for e in sector_list if e not in etfs]
                if spec.sector_hard_fail:
                    raise (StrictDataUnavailable if execution else RuntimeError)(
                        f"[{spec.market_id}] 板块 ETF 覆盖率不足（{len(etfs)}/{len(sector_list)} < 2/3，"
                        f"缺 {missing}）：SHS 主线判定地基不全，诚实失败")
                report["sector_breadth_missing"] = (
                    f"板块广度数据不足（{len(etfs)}/{len(sector_list)}，缺 {missing}）→ "
                    "SHS 板块维度按缺失披露（D2：宁可缺失不可编造）")
                logger.warning("[%s] %s", spec.market_id, report["sector_breadth_missing"])
            stale = [t for t, df in stocks.items()
                     if df is not None and len(df)
                     and (spy_last - df.index[-1].date()).days > 5]
            for t in stale:
                del stocks[t]
            if stale:
                logger.warning("剔除 %d 只陈旧/停牌标的（末根滞后 %s 超 5 天）: %s%s",
                               len(stale), idx_name, stale[:10], "..." if len(stale) > 10 else "")
            report["stale_removed"] = stale
            report["etf_coverage"] = f"{len(etfs)}/{len(sector_list)}"
            return report

        # ---------- 数据准备 ----------
        days = 420
        market_data: dict = {"benchmark_labels": benchmark_labels}
        with tracer.step("data.prepare", {"universe": universe, "benchmarks": bmk,
                         "sector_symbols": spec.sector_symbols, "days": days,
                         "trade_date": trade_date, "scan_filters": spec.scan_filters(),
                         **({"market_close": close_binding} if close_binding else {})}) as stage:
            idx_sym = bmk["index"]
            try:
                spy = _single_with_fallback(provider, "ohlcv", idx_sym, days=days,
                                             **({"close_cutoff": session} if session else {}))
            except (EvidenceWriteError, ExecutionContractError, StrictObservationError, StrictDataStale):
                raise
            except Exception as exc:
                # D2 诚实失败：该市场基准（指数）全断 → 当日中止，其余市场不受影响
                raise (StrictDataUnavailable if execution else RuntimeError)(
                    f"[{spec.market_id}] {idx_sym} 基准数据获取失败（含降级源），"
                    f"该市场当日诚实失败，MRS 无法计算: {exc}") from exc
            market_data["spy"] = spy
            # 利率基准：US 硬依赖（现状行为）；CN/HK 缺失 → 记 None 走再归一化
            try:
                market_data["tnx"] = _single_with_fallback(
                    provider, "rate_yield_for", bmk["rate"], days=days,
                    **({"close_cutoff": macro_dates["tnx"]} if session else {}))
            except (EvidenceWriteError, ExecutionContractError, StrictObservationError):
                raise
            except Exception as exc:
                if spec.benchmark_hard_fail:
                    raise (StrictDataUnavailable if execution else RuntimeError)(
                        f"[{spec.market_id}] {bmk['rate']} 数据获取失败（含降级源），"
                        f"MRS 无法计算: {exc}") from exc
                logger.warning("[%s] 利率基准 %s 不可得（%s）→ 宏观维缺失再归一化",
                               spec.market_id, bmk["rate"], str(exc))
                market_data["tnx"] = None
            # 波动率基准：同上
            try:
                market_data["vix"] = _single_with_fallback(
                    provider, "vol_index_for", bmk["vol"], days=days,
                    **({"close_cutoff": macro_dates["vix"]} if session else {}))
            except (EvidenceWriteError, ExecutionContractError, StrictObservationError):
                raise
            except Exception as exc:
                if spec.benchmark_hard_fail:
                    raise (StrictDataUnavailable if execution else RuntimeError)(
                        f"[{spec.market_id}] {bmk['vol']} 数据获取失败（含降级源），"
                        f"MRS 无法计算: {exc}") from exc
                logger.warning("[%s] 波动率基准 %s 不可得（%s）→ 情绪维缺失再归一化",
                               spec.market_id, bmk["vol"], str(exc))
                market_data["vix"] = None
            try:
                market_data["vix9d"] = (_single_with_fallback(
                    provider, "vol_index_for", bmk["vol_short"], days=days,
                    **({"close_cutoff": macro_dates["vix9d"]} if session else {}))
                    if bmk.get("vol_short") else None)
            except (EvidenceWriteError, ExecutionContractError, StrictObservationError):
                raise
            except Exception:
                market_data["vix9d"] = None

            sector_etfs = _batch_with_fallback(provider, spec.sector_symbols, days,
                **({"label": "signal_etfs", "close_cutoff": session,
                    "minimum": (len(spec.sector_symbols) * 2 + 2) // 3} if session else {}))
            market_data["sector_etfs"] = sector_etfs

            # full 模式两级拉取：先用 30 日短历史做流动性预筛，再对幸存者拉全历史
            prefilter_note = ""
            scan_filt = spec.scan_filters()
            if len(universe) > config.FULL_PREFILTER_THRESHOLD:
                logger.info("池规模 %d > %d，启动流动性预筛（%d 日短历史）",
                            len(universe), config.FULL_PREFILTER_THRESHOLD,
                            config.FULL_PREFILTER_DAYS)
                light = _batch_with_fallback(provider, universe, config.FULL_PREFILTER_DAYS,
                    **({"label": "signal_prefilter", "close_cutoff": session} if session else {}))
                survivors: list[tuple[str, float]] = []
                for t, df in light.items():
                    if len(df) < 15:
                        continue
                    px = float(df["Close"].iloc[-1])
                    adv = float((df["Close"] * df["Volume"]).tail(20).mean())
                    if px >= scan_filt["min_price"] and adv >= scan_filt["min_adv"]:
                        survivors.append((t, adv))
                survivors.sort(key=lambda x: x[1], reverse=True)
                kept = survivors[: config.FULL_HEAVY_CAP]
                prefilter_note = (f"预筛 {len(universe)}→{len(light)}→流动性通过 "
                                  f"{len(survivors)}→重量级拉取 {len(kept)}")
                logger.info(prefilter_note)
                universe = [t for t, _ in kept]

            # 全市场股票数据（扫描 + 产业链 + 广度共用一份）
            # S4：US 产业链为美股映射，仅 US 市场并入拉取；CN/HK 链覆盖率如实披露为 0
            chain_tickers: set[str] = set()
            if spec.market_id == "us":
                from .chains import CHAINS
                for c in CHAINS.values():
                    for link in ("upstream", "midstream", "downstream"):
                        chain_tickers.update(c[link]["tickers"])
            all_tickers = sorted(set(universe) | chain_tickers)
            stock_data = _batch_with_fallback(provider, all_tickers, days,
                **({"label": "signal_stocks", "close_cutoff": session} if session else {}))
            if session and not stock_data:
                raise StrictDataUnavailable(f"缺少已完成交易日 {session} 的可用股票收盘数据")
            market_data["stock_ohlcv"] = stock_data
            # v6.1：真实性/时效性闸门——充足性、新鲜度、陈旧标的剔除（诚实失败优于脏数据）
            freshness = _freshness_gate(spy, market_data["tnx"], market_data["vix"],
                                        sector_etfs, stock_data)
            if close_binding:
                from .agent_evidence import normalize
                freshness["market_close"] = {
                    **close_binding,
                    "etf_missing": sorted(set(spec.sector_symbols) - sector_etfs.keys()),
                    "stock_missing": sorted(set(all_tickers) - stock_data.keys()),
                    "macro_last_bars": {name: (values.index[-1].date().isoformat()
                        if (values := market_data[name]) is not None else None) for name in macro_dates},
                    "benchmark_chart": {"SPY": "signal_benchmark", "QQQ": "not_collected"},
                }
                execution.signal_benchmark_chart = close_chart(normalize(spy))
            market_data["freshness"] = freshness
            market_data["universe_closes"] = {t: df["Close"] for t, df in stock_data.items()}
            logger.info("数据准备完成: 股票 %d/%d, 板块ETF %d",
                        len(stock_data), len(all_tickers), len(sector_etfs))

            stage.output({"market_data": market_data, "universe": universe, "all_tickers": all_tickers,
                          **({"security_roster": security_roster} if security_roster else {}),
                          "prefilter": prefilter_note, "lineage": list(_LINEAGE), "data_issues": list(_DATA_ISSUES)})

        return (market_data, universe, all_tickers, stock_data, sector_etfs, spy,
                scan_filt, prefilter_note, session, close_binding)

    intelligence = bool(execution is not None and getattr(execution, "intelligence", False))
    intelligence_state = None
    if intelligence:
        from .intelligence_workflow import build_frontier
        (market_data, universe, all_tickers, stock_data, sector_etfs, spy,
         scan_filt, prefilter_note, session, close_binding) = prepare_market(universe)
        market_ref = dict(execution._previous_stage_ref)
        with tracer.step("intelligence.frontier", {"market_snapshot_ref": market_ref,
                "universe": universe, "portfolio_state": portfolio_state}) as frontier_stage:
            frontier = build_frontier(market_data, universe, security_roster["symbols"],
                as_of=execution.admitted_at, market_snapshot_ref=market_ref,
                portfolio_state=portfolio_state, scan_filters=scan_filt, sector_symbols=spec.sector_symbols)
            frontier_stage.output(frontier)

    with tracer.step("search.collect", {"chains": CHAIN_TOPICS, "sectors": SECTOR_TOPICS,
                     "ah": config.AH_TOPICS, "ah_enabled": ah_enabled, "limit_per_query": 4,
                     "deep": True, "source_allowlist": execution.policy.search_source_names if execution else None}) as stage:
        if required:
            all_topics = {**CHAIN_TOPICS, **SECTOR_TOPICS}
            if intelligence:
                intelligence_state = semantic.Acquisition(llm, frontier, all_topics)
                raw_docs = intelligence_state.collect()
            else:
                raw_docs = semantic.collect(llm, all_topics, trade_date)
            search_cleanup, batches = {"native_calls_completed": len(llm.search_receipts)}, []
        else:
            hub = SearchHub(demo=demo_mode, execution=execution) if execution else SearchHub(demo=demo_mode)
        # 主题集注册（SearchHub 同款机制）：科技六子链 + 全领域八主题——每个板块
        # 都要有自己的情报输入，否则非科技板块的 LLM 叙事维度被架空（情报偏科即评分偏科）。
        # S3：AH（A股/港股）主题默认关闭（config.AH_TOPICS_ENABLED，S4 多市场启用），
        # 关闭时整体缺席、不产生任何网络调用，开关状态写入日报披露。
            hub.register_topic_set("chains", CHAIN_TOPICS)
            hub.register_topic_set("sectors", SECTOR_TOPICS)
            hub.register_topic_set("ah", config.AH_TOPICS, enabled=ah_enabled)
            raw_docs = hub.gather_topic_sets(limit_per_query=4, deep=True)
            search_cleanup = (execution.call_io("search.workers.settle",
                          {"collection_budget_s": hub.budget, "cleanup_budget_s": hub.cleanup_budget},
                          hub.settle_workers) if execution is not None else {})
            all_topics = hub.active_topics()
            batches = hub.batches if execution else []
        logger.info("search.collect: %d 篇原始文档（%d 主题；AH主题=%s）",
                    len(raw_docs), len(all_topics),
                    "启用" if ah_enabled else "关闭")

        stage.output({"documents": raw_docs, "topics": all_topics,
                      "batches": batches, "cleanup": search_cleanup,
                      **({"acquisition": intelligence_state.snapshot()} if intelligence else {})})

    with tracer.step("clean.rule_base", {"documents": raw_docs,
                     **({"source_content_contract": semantic.SOURCE_CONTENT_CONTRACT} if required else {})}) as stage:
        base_docs = rule_base_clean(raw_docs, preserve_source=required)
        logger.info("clean.rule_base: %d→%d（去重/格式校验）", len(raw_docs), len(base_docs))

        stage.output({"documents": base_docs})

    with tracer.step("clean.llm_semantic", {"documents": base_docs,
                     **({"assessment": assessment} if required else {})}) as stage:
        cleaned = (semantic.clean(llm, base_docs, acquisition=intelligence_state) if intelligence else
                   semantic.clean(llm, base_docs) if required else unwrap_cleaned(llm_semantic_clean(base_docs, llm, tracer)))
        degraded_n = sum(1 for c in cleaned if c.degraded)
        logger.info("clean.llm_semantic: %d 篇（语义缺失 %d）", len(cleaned), degraded_n)

        stage.output({"documents": cleaned, "degraded_count": degraded_n,
                      **({"acquisition": intelligence_state.snapshot()} if intelligence else {})})

    # S3 数据层：交叉验证（规则层调度，D9 不引入 LLM）。关键事件类文档需
    # ≥2 个不同源（且至少一个 ≤T2）佐证；未达标标记 corroborated=False——
    # 不删除（透传纪律），但叙事/舆情打分输入只使用 corroborated=True 的集合；
    # 被降级文档在决策侧仅作背景参考（tech.risk / decision.debate 仍可见全集）。
    from .search.event_identity import EVENT_CONTRACT, validate_events
    if intelligence:
        from .search.event_identity import STRUCTURED_EVENT_CONTRACT as EVENT_CONTRACT
    event_cutoff = execution.admitted_at if required else None
    event_symbols = security_roster["symbols"] if security_roster else {ticker: "us:" + ticker for ticker in universe}
    with tracer.step("clean.cross_validate", {"documents": cleaned,
                     **({"event_contract": EVENT_CONTRACT, "security_ids": event_symbols}
                        if event_cutoff is not None else {})}) as stage:
        from .search.credibility import CrossValidator, scoring_docs
        cv_stats = (intelligence_state.validate(cleaned) if intelligence else
                    validate_events(cleaned, event_cutoff, security_ids=event_symbols) if event_cutoff is not None
                    else CrossValidator().validate(cleaned))
        score_docs = scoring_docs(cleaned)
        logger.info("clean.cross_validate: 总 %d 篇｜关键事件 %d｜通过 %d｜降级 %d"
                    "（LLM标注缺失 %d｜缺发布时间 %d）",
                    cv_stats["total"], cv_stats["key_event_docs"],
                    cv_stats["corroborated"], cv_stats["downgraded"],
                    cv_stats["llm_missing"], cv_stats["missing_published_at"])

        stage.output({"documents": cleaned, "scoring_documents": score_docs, "stats": cv_stats})

    if intelligence:
        with tracer.step("intelligence.freeze", {"frontier_hash": frontier["manifest_sha256"],
                "documents": cleaned, "stats": cv_stats}) as frozen_stage:
            intelligence_bundle = intelligence_state.freeze(cleaned, cv_stats)
            frozen_stage.output(intelligence_bundle)

    # WFA 调优参数：默认【不加载】——tuned_params.json 属于上一轮生产残留，
    # 零基线纪律下每轮从零开始；仅当调用方显式 use_tuned=True 时才覆盖默认闸门。
    tuned_note = ""
    if use_tuned:
        try:
            from .backtest import apply_tuned_params
            applied = apply_tuned_params()
            if applied:
                tuned_note = f"已加载 WFA 调优参数（显式 --use-tuned）: {applied}"
                logger.info(tuned_note)
        except Exception:
            pass
    logger.info("=== Pipeline 启动 market=%s provider=%s universe=%s(%d) ===",
                spec.market_id, provider.name, universe_mode, len(universe))

    if not intelligence:
        (market_data, universe, all_tickers, stock_data, sector_etfs, spy,
         scan_filt, prefilter_note, session, close_binding) = prepare_market(universe)

    # ================= 科技股产业链专项子集群 =================
    # （放在数据准备之后：monitor 复用已下载行情，仅全球联动标的单独拉取）
    monitor_excluded = (frozenset(t for chain in TECH_SUBCHAINS.values()
        for tickers in chain["links"].values() for t in tickers if requires_foreign_calendar(t))
        if session else frozenset())
    with tracer.step("tech.monitor", {"prefetched": stock_data, "provider": provider.name,
                     **({"market_close": close_binding, "excluded": sorted(monitor_excluded)}
                        if session else {})}) as stage:
        if monitor_excluded:
            _data_issue(provider, "ohlcv_batch", sorted(monitor_excluded), "linkage_calendar_unverified",
                        label="signal_tech_monitor")
        tech_monitor = TechChainMonitorAgent(provider).execute(
            tracer, prefetched=stock_data, **({"excluded": monitor_excluded} if session else {}))
        stage.output(tech_monitor)
    linkage_frozen = ({"SPY": spy, **{symbol: stock_data[symbol]
                       for symbol in CycleLinkageAgent.LEADERS if symbol in stock_data}} if session else None)
    with tracer.step("tech.cycle_linkage", {"leaders": CycleLinkageAgent.LEADERS, "provider": provider.name,
                     **({"market_close": close_binding, "prefetched": sorted(linkage_frozen)}
                        if linkage_frozen is not None else {})}) as stage:
        linkage = CycleLinkageAgent(provider)
        if linkage_frozen is None:
            tech_linkage = linkage.execute(tracer)
        else:
            def fetch_leader(symbol):
                if requires_foreign_calendar(symbol):
                    # These exchanges need their own local dates and holidays.
                    _data_issue(provider, "ohlcv", [symbol], "linkage_calendar_unverified",
                                label="signal_linkage")
                    return None
                return _single_with_fallback(provider, "ohlcv", symbol, days=90, close_cutoff=session)

            tech_linkage = linkage.execute(tracer, prefetched=linkage_frozen, fetch=fetch_leader)
            covered = {row.ticker for row in tech_linkage}
            missing = [symbol for symbol in CycleLinkageAgent.LEADERS if symbol not in covered]
            if missing:
                _data_issue(provider, "ohlcv", missing, "completed_session_linkage_missing", label="signal_linkage")
        stage.output(tech_linkage)
    with tracer.step("tech.sentiment", {"documents": score_docs,
                     **({"assessment": assessment} if required else {})}) as stage:
        tech_sentiment = semantic.sentiment(llm, score_docs) if required else ChainSentimentAgent(llm).execute(score_docs, tracer)
        stage.output(tech_sentiment)
    risk_docs = score_docs if event_cutoff is not None else cleaned
    risk_input = {"risk_coverage": risk_coverage(risk_docs if intelligence else len(risk_docs))} if event_cutoff is not None else {}
    with tracer.step("tech.risk", {"documents": risk_docs, **risk_input,
                     **({"assessment": assessment} if required else {})}) as stage:
        tech_alerts = semantic.risk(llm, risk_docs) if required else ChainRiskAgent(llm).execute(risk_docs, tracer)
        stage.output(tech_alerts)
    if intelligence:
        risk_input = {"risk_coverage": semantic.assessed_risk_coverage(llm, risk_docs)}
    with tracer.step("tech.fusion", {"monitor": tech_monitor, "linkage": tech_linkage,
                     "sentiment": tech_sentiment, "alerts": tech_alerts, **risk_input}) as stage:
        tech_signals = TechChainFusionAgent().execute(
            tech_monitor, tech_linkage, tech_sentiment, tech_alerts, tracer,
            risk_evidence_available=event_cutoff is None or bool(risk_docs),
            **({"risk_coverage_by_chain": semantic.assessed_risk_coverage(llm, risk_docs)["targets"]}
               if intelligence else {}))
        logger.info("tech.fusion: %s",
                    [(s.chain_id, s.prosperity, s.risk_level, s.bonus_hint) for s in tech_signals])

        stage.output(tech_signals)

    # ================= SHS 叙事维度（LLM 化） =================
    narrative_llm: dict = {}
    with tracer.step("sector.narrative", {"documents": score_docs, "sector_symbols": spec.sector_symbols,
                     **({"assessment": assessment} if required else {})}) as stage:
        narr_res = semantic.narrative(llm, score_docs, spec.sector_symbols) if required else NarrativeAgent(llm).execute(score_docs, spec.sector_symbols, tracer)
        if not isinstance(narr_res, Passthrough) and (intelligence or event_cutoff is None or score_docs):
            narrative_llm = narr_res

        stage.output({"raw_result": narr_res, "narrative": narrative_llm})

    # v6.3：链映射覆盖率披露——无链映射的候选在 L4 只能走轻仓通道
    # （SHS 钉 5.0），覆盖率本身就是公平性指标，必须显性化
    from .chains import TICKER_TO_CHAIN as _T2C
    _mapped = sum(1 for t in all_tickers if t in _T2C)
    chain_coverage = f"{_mapped}/{len(all_tickers)}（{_mapped / max(len(all_tickers), 1):.0%}）"
    logger.info("产业链映射覆盖率: %s", chain_coverage)

    context: dict = {
        "market_data": market_data,
        "trade_date": trade_date,
        "chain_coverage": chain_coverage,
        # S4 多市场：市场规格 / 扫描过滤 / 轻仓毕业门槛（agents 只读）
        "market_spec": spec,
        "scan_filters": scan_filt,
        "market_grad": market_grad,
        # v6.0：事件日历（白皮书§11 事件风险管理的机器执行）
        "event_calendar": event_calendar or EventCalendar(),
        "options_store": options_store,
        # 科技链子集群标准化汇入（核心因子之一）
        "tech_signals": [s.to_dict() for s in tech_signals],
        "tech_bonus_map": signals_to_bonus_map(tech_signals),
        "tech_signal_map": {s.main_chain: s for s in tech_signals},
        # SHS 叙事维度 LLM 注入（空 dict = 透传，维度剔除再归一化）
        "narrative_llm": narrative_llm,
    }
    from .activity import ACTIVE_POLICY_ID, ACTIVE_PROFILE
    active = execution is not None and execution.policy.policy_id == ACTIVE_POLICY_ID
    if execution is not None:
        context.update(paper_execution_budgeted=True, portfolio_state=portfolio_state)
    if required:
        context["capture_scan_decisions"] = True
    if active:
        context.update(activity_profile=ACTIVE_PROFILE)

    # ---------- 六层流水线 ----------
    with tracer.step("layer1.mrs", {"market_data": market_data}) as stage:
        mrs = MRSAgent(provider).execute(context)
        stage.output(mrs)
    with tracer.step("layer2.sector", {"market_data": market_data, "narrative": narrative_llm}) as stage:
        sectors = SectorAgent(provider).execute(context)
        stage.output({"sectors": sectors, "sector_map": context.get("sector_map", {})})
    with tracer.step("layer2b.chain", {"spy": spy, "stock_ohlcv": stock_data}) as stage:
        chains = ChainCycleAgent(provider).execute(context)
        stage.output({"chains": chains, "chain_map": context.get("chain_map", {})})
    with tracer.step("layer0.scan", {"universe": universe, "top_n": top_n,
                     "market_data": market_data, "scan_filters": scan_filt, "sectors": sectors,
                     **({"include_entry_setups": True} if required else {})}) as stage:
        scanner = UniverseScannerAgent(provider, universe, top_n=top_n,
                                       include_entry_setups=required)
        scan_started = time.perf_counter()
        candidates = scanner.execute(context)
        if intelligence:
            from .intelligence_workflow import qualify_candidates
            exclusions = qualify_candidates(context, frontier)
            for row in exclusions:
                audit_row = context["scan_decisions"][row["symbol"]]
                audit_row["market_selection"] = audit_row.get("selection")
                audit_row["selection"] = "research_missing"
            candidates = context["watchlist"]
        stage.output({"candidates": candidates, "scan_stats": context["scan_stats"],
                      **({"scan_decisions": context["scan_decisions"],
                          "scan_elapsed_ms": round((time.perf_counter() - scan_started) * 1000, 1)}
                         if required else {})})
    with tracer.step("layer3.tss", {"watchlist": candidates, "market_data": market_data,
                     "chain_map": context.get("chain_map", {}), "sector_map": context.get("sector_map", {}),
                     "tech_bonus_map": context["tech_bonus_map"], "tech_signal_map": context["tech_signal_map"],
                     "trade_date": trade_date}) as stage:
        candidates = TSSAgent(provider).execute(context)
        stage.output({"watchlist": candidates, "options_history": options_store.observed if options_store else {}})
    with tracer.step("layer4.risk", {"watchlist": candidates, "mrs": mrs, "sectors": sectors,
                     "chains": chains, "market_grad": market_grad, "market": spec.to_dict(),
                     "trade_date": trade_date, "account_usd": account_usd, "max_picks": max_picks,
                     "calendar": event_calendar.to_dict() if event_calendar else None,
                     "tech_signal_map": context["tech_signal_map"]}) as stage:
        picks = RiskManagerAgent(provider, account_usd=account_usd,
                                 max_picks=max_picks).execute(context)

        stage.output({key: context.get(key) for key in
                      ("picks", "pick_rationale", "action", "market_view", "notes", "currency_policy", "compliance", "rejections", "entry_budget")})

    # ================= 多空辩论证据层（v6.3 S2，桥水 AIA 辩论制） =================
    # 铁律：辩论在 L4 闸门产出【之后】执行，只读 picks/rationale/watchlist，
    # 绝不回写任何分数与闸门输出；产出仅作交易卡片"第六段"进报告层。
    # 回测路径不经过本 pipeline（无未来函数：历史回放禁止注入 LLM）。
    if required:
        approved_symbols = [pick.ticker for pick in picks]
        decision_symbols = approved_symbols + [candidate.ticker for candidate in context.get("watchlist", [])
                                               if candidate.ticker not in approved_symbols][:5]
        decision_asof = semantic.market_asof(market_data, decision_symbols, assessment)
        decision_market_evidence = semantic.market_snapshots(market_data, decision_symbols, assessment)
    with tracer.step("decision.debate", {"enabled": config.DEBATE_ENABLED, "picks": picks,
                     "rationale": context.get("pick_rationale") if required else context.get("pick_rationale", {}),
                     "watchlist": context.get("watchlist", []),
                     "action": context.get("action", "HOLD"), "documents": cleaned,
                     **({"mrs": context.get("mrs"), "sectors": context.get("sectors"),
                         "rejections": context.get("rejections"), "account_usd": account_usd,
                         "assessment": assessment, "market_asof": decision_asof,
                         "market_evidence": decision_market_evidence} if required else {})}) as stage:
        from .agents.debate_agent import DebateAgent, select_debate_targets
        debate_raw: dict = {"status": "disabled", "triggered": [], "evidence": {}}
        if required:
            debate_raw = semantic.decision(llm, context, cleaned, account_usd)
        elif config.DEBATE_ENABLED:
            targets = select_debate_targets(
                context.get("picks", []), context.get("pick_rationale", {}),
                context.get("watchlist", []), context.get("action", "HOLD"))
            debate_raw["triggered"] = [t["symbol"] for t in targets]
            if targets:
                res = DebateAgent(llm).execute(targets, cleaned, tracer)
                if isinstance(res, Passthrough):
                    debate_raw["status"] = "passthrough"   # 报告标注"本轮无辩论证据"
                else:
                    debate_raw["status"] = "ok"
                    debate_raw["evidence"] = {s: e.to_dict() for s, e in res.items()}
            else:
                debate_raw["status"] = "no_target"         # 灰区外，本轮不辩论
        context["debate"] = debate_raw
        logger.info("decision.debate: status=%s triggered=%s",
                    debate_raw["status"], debate_raw["triggered"])

        stage.output(debate_raw)

    # ================= 统计校准层（v6.3 S2，迭代层·只进报告不改闸门） =================
    # 会计账白名单（与 journal 同级）：只读已结算台账，输出仅进报告披露，
    # 绝不进入决策输入（代码层隔离，见 calibration.py 头注）。
    with tracer.step("iteration.calibration", {"currency_policy": context["currency_policy"],
                     "accounting_blocked_reason": accounting_blocked_reason,
                     "journal_path": execution.paths.journal_path if execution else None,
                     "samples_path": execution.paths.calibration_path if execution else None}) as stage:
        from .calibration import CalibrationLayer
        try:
            if context["currency_policy"]["execution_blocked"] or accounting_blocked_reason:
                calibration_summary = {"status": "skipped",
                                       "reason": (context["currency_policy"]["reason"]
                                                  or accounting_blocked_reason)}
            else:
                if execution is not None:
                    from .ledger_io import read_json_strict
                    journal_input = execution.call_io("calibration.journal.read",
                        {"path": execution.paths.journal_path},
                        lambda: read_json_strict(execution.paths.journal_path))
                    samples_before = execution.call_io("calibration.samples.read",
                        {"path": execution.paths.calibration_path},
                        lambda: read_json_strict(execution.paths.calibration_path))
                    if execution.authoritative_paper:
                        calibration = CalibrationLayer()
                        samples = calibration.collect_samples(journal_input, paper_only=True)
                        calibration_summary = {**calibration.summarize(samples),
                                               "basis": "before_paper_execution"}
                    else:
                        calibration_summary = execution.call_io("calibration.run",
                            {"journal": journal_input, "previous_samples": samples_before,
                             "journal_path": execution.paths.journal_path,
                             "samples_path": execution.paths.calibration_path},
                            lambda: CalibrationLayer(journal_path=str(execution.paths.journal_path),
                                samples_path=str(execution.paths.calibration_path)).run())
                        execution.call_io("calibration.samples.readback",
                            {"path": execution.paths.calibration_path},
                            lambda: read_json_strict(execution.paths.calibration_path))
                else:
                    calibration_summary = CalibrationLayer().run()
        except Exception as exc:
            if execution is not None:
                raise
            # 注册表约定：本环节不可透传——失败则跳过披露并记录（不阻塞主链路）
            logger.warning("iteration.calibration 失败，跳过披露并记录: %s", exc)
            calibration_summary = {"status": "skipped", "reason": str(exc)}
        context["calibration"] = calibration_summary
        logger.info("iteration.calibration: status=%s n=%s",
                    calibration_summary.get("status"), calibration_summary.get("n"))

        stage.output(calibration_summary)

    with tracer.step("report.emit", {key: value for key, value in context.items()
                     if key not in ("market_data", "market_spec", "event_calendar", "options_store",
                                    "capture_scan_decisions", "scan_decisions")}) as stage:
        notes = context.get("notes", [])
        if tuned_note:
            notes = notes + [tuned_note]
        result = PipelineResult(
            trade_date=trade_date,
            provider=_provider_source(provider),
            mrs=context.get("mrs"),
            sectors=context.get("sectors", []),
            chains=context.get("chains", []),
            watchlist=context.get("watchlist", []),
            picks=context.get("picks", []),
            action=context.get("action", "HOLD"),
            market_view=context.get("market_view", ""),
            notes=notes,
            raw={
                "scan_stats": context.get("scan_stats", {}),
                "elapsed_s": round(time.time() - started, 1),
                "universe_mode": universe_mode,
                "universe_source": universe_source,
                **({"security_roster": security_roster} if security_roster else {}),
                "account_usd": account_usd,
                "account_currency": "USD",
                "currency_policy": context["currency_policy"],
                "pick_rationale": context.get("pick_rationale", {}),
                "rejections": context.get("rejections", {}),
                "activity_profile": ACTIVE_PROFILE if active else None,
                "paper_execution_budgeted": execution is not None,
                "paper_execution_version": ("tiger.paper-execution.v1" if execution and execution.authoritative_paper
                                            else "signal-budgeted-open-v1" if execution else "legacy"),
                "entry_budget": context.get("entry_budget"),
                "source_lineage": list(_LINEAGE),
                "provider_health": ({name: dict(health) for name, health in _HEALTH.items()}
                                    if execution is not None else dict(_HEALTH)),
                "provider_name": provider.name,
                "synthetic": demo_mode,
                "data_issues": list(_DATA_ISSUES),
                "freshness": market_data.get("freshness", {}),
                "chain_coverage": context.get("chain_coverage", ""),
                "data_coverage": f"{len(stock_data)}/{len(all_tickers)}",
                "prefilter": prefilter_note,
                "tech_signals": context.get("tech_signals", []),
                "docs_collected": len(intelligence_state.documents) if intelligence else len(raw_docs),
                "docs_cleaned": len(cleaned),
                "docs_semantic_degraded": degraded_n,
                # v6.3 S3：交叉验证统计与 AH 主题开关（数据层披露）
                "cross_validation": cv_stats,
                **({"intelligence": {**intelligence_state.disclosure(),
                    "bundle": {key: intelligence_bundle[key] for key in
                        ("bundle_id", "manifest_sha256", "state", "missing_required", "usage",
                         "frozen_at", "frontier_manifest_hash")},
                    "frontier": {key: frontier[key] for key in
                        ("contract", "manifest_sha256", "symbols", "selection_reasons", "missing", "sector_gaps")},
                    "exclusions": context.get("intelligence_exclusions", [])}} if intelligence else {}),
                "ah_topics_enabled": ah_enabled,
                # v6.3 S4：市场标识 / 合规校验记录 / 轻仓毕业状态（日报披露）
                "market": spec.to_dict(),
                "compliance": context.get("compliance", []),
                "market_grad": market_grad,
                # v6.3 S2：辩论证据与校准摘要（只读证据/披露，不含决策输入）
                "debate": context.get("debate", {}),
                "calibration": context.get("calibration", {}),
                **({"model": semantic.model_summary(llm),
                    "action_summary": context["action_summary"],
                    "execution_decisions": context["execution_decisions"]} if required else {}),
            },
        )
        stage.output(result)
    # 红线 1：全链路环节完整性强制校验（缺环节 = 系统性事故，立即停止）
    tracer.assert_complete()
    # S6：review.daily 属延迟环节（依赖 main.daily 尾部的 journal 落账/结算），
    # 记 deferred 占位；main.daily 由 review.chief 实际执行后改写为 executed。
    tracer.mark_deferred("review.daily",
                         "复盘纪要由 main.daily 尾部 review.chief 生成并补账")
    result.raw["redline"] = tracer.summary()
    logger.info("=== Pipeline 完成 %.1fs action=%s picks=%d ===",
                time.time() - started, result.action, len(result.picks))
    return result
