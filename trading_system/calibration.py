"""统计校准层（v6.3 S2）— 信号分数 → 实际结果的校准摘要。

定位（调研一亮点：校准层 + 样本门槛）：
  从 journal 已结算记录提取"信号分数快照（TSS_final / MRS*）→ 实际结果（R）"
  样本，分桶统计实际胜率 / 期望 R / 样本数，并检测分数与结果的单调性。

样本门槛（迭代诚实 D7 的机器执行）：
  总样本 < CALIBRATION_MIN_SAMPLES（默认 50）时只输出区间表述
  （桶胜率 ± Wilson 95% 区间），报告固定披露"校准样本积累中（n/50）"，
  不产出校准曲线——样本不足时沉默比宣传更专业；≥50 才输出校准曲线点。

隔离性声明（会计账白名单，与 journal 同级）：
  - 本模块【只读】已结算台账（status=="closed" 且 r 非空），
    样本落盘 CALIBRATION_SAMPLES_PATH（零基线白名单，见 state.py）；
  - 输出【只进报告层】（report.py "统计校准"小节），
    绝不进入决策输入——本模块不 import gate/agents/pipeline 决策链路，
    pipeline 中仅以 iteration.calibration 环节调用并把摘要写入 raw 披露，
    任何分数/闸门计算都不得读取本模块（pytest 静态审查锁定）。
"""

from __future__ import annotations

import json
import math
from pathlib import Path

from . import config
from .currency import require_usd_records
from .ledger_io import write_json_atomic

_WILSON_Z = 1.96     # 95% 置信区间


def wilson_interval(wins: int, n: int, z: float = _WILSON_Z) -> tuple[float, float]:
    """Wilson score 区间（小样本下比正态近似稳健的胜率置信区间）。"""
    if n <= 0:
        return (0.0, 1.0)
    p = wins / n
    denom = 1.0 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (round(max(0.0, center - half), 4), round(min(1.0, center + half), 4))


class CalibrationLayer:
    """校准样本库 + 校准摘要（只读台账、只进报告）。"""

    def __init__(self, journal_path: str | None = None,
                 samples_path: str | None = None,
                 min_samples: int | None = None,
                 buckets: list[tuple[float, float]] | None = None):
        self.journal_path = Path(journal_path or config.CALIBRATION_JOURNAL_PATH)
        self.samples_path = Path(samples_path or config.CALIBRATION_SAMPLES_PATH)
        self.min_samples = (config.CALIBRATION_MIN_SAMPLES
                            if min_samples is None else min_samples)
        self.buckets = buckets or list(config.CALIBRATION_BUCKETS)

    # ------------------------------------------------------------ 样本库

    def collect_samples(self, records: list[dict], *, paper_only: bool = False) -> list[dict]:
        """从 journal 记录提取校准样本。【只读已结算记录】：
        status=="closed" 且 r 非空（未结算/作废/悬挂一律不进入样本库）。"""
        if not isinstance(records, list) or any(
            not isinstance(rec, dict)
            or not all(isinstance(rec.get(key), str) and rec[key]
                       for key in ("date", "ticker", "status"))
            for rec in records
        ):
            raise ValueError(f"校准历史 {self.journal_path} 结构无效，已停止以保留原样本库")
        require_usd_records(records, scope="校准历史")
        samples: list[dict] = []
        seen: set[tuple] = set()
        for index, rec in enumerate(records):
            if rec.get("status") != "closed" or rec.get("r") is None:
                continue
            if paper_only and (rec.get("accounting_basis") != "paper_execution"
                               or not rec.get("signal_id")
                               or not {"filled", "closed"}.issubset(rec.get("paper_event_ids", {}))):
                continue
            for field in ("r", "tss_final", "mrs_star"):
                value = rec.get(field)
                if value is None:  # 旧台账允许分数缺失，不能补造分数。
                    continue
                try:
                    valid = (isinstance(value, (int, float)) and not isinstance(value, bool)
                             and math.isfinite(value))
                except OverflowError:
                    valid = False
                if not valid:
                    raise ValueError(
                        f"校准历史 {self.journal_path} 第 {index + 1} 条 {field} "
                        "不是有限数值，已停止以保留原样本库")
            key = rec["signal_id"] if paper_only else (rec.get("date"), rec.get("ticker"))
            if key in seen:
                continue
            seen.add(key)
            samples.append({
                "date": rec.get("date"), "ticker": rec.get("ticker"),
                "tss_final": rec.get("tss_final"),
                "mrs_star": rec.get("mrs_star"),
                "r": rec.get("r"), "win": bool(rec.get("r", 0) > 0),
                **({"signal_id": rec["signal_id"], "accounting_basis": "paper_execution",
                    "closing_event_id": rec["paper_event_ids"]["closed"]} if paper_only else {}),
            })
        return samples

    def save_samples(self, samples: list[dict]) -> None:
        require_usd_records(samples, scope="校准样本保存")
        write_json_atomic(self.samples_path, samples, indent=1)

    # ------------------------------------------------------------ 摘要

    def run(self, *, paper_only: bool = False) -> dict:
        """读 journal → 验证并计算摘要 → 原子更新样本库，失败保留旧历史。"""
        try:
            records = json.loads(self.journal_path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            # 首次运行可以没有 journal；已有历史时不能把源文件丢失当成零样本。
            try:
                previous = json.loads(self.samples_path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                records = []
            except (OSError, ValueError) as sample_exc:
                raise ValueError(
                    f"校准历史 {self.journal_path} 缺失，无法验证已有样本库"
                    f"（{type(sample_exc).__name__}），已保留原样本库") from sample_exc
            else:
                if previous != []:
                    raise ValueError(
                        f"校准历史 {self.journal_path} 缺失，已停止以保留原样本库") from exc
                return self.summarize([])  # 已验证的空历史无需重写。
        except (OSError, ValueError) as exc:
            raise ValueError(
                f"校准历史 {self.journal_path} 读取或解析失败"
                f"（{type(exc).__name__}），已停止以保留原样本库") from exc
        samples = self.collect_samples(records, paper_only=paper_only)
        summary = self.summarize(samples)
        self.save_samples(samples)
        return summary

    def summarize(self, samples: list[dict]) -> dict:
        """分桶统计 + 样本门槛降级 + 单调性检测。"""
        require_usd_records(samples, scope="校准样本统计")
        n = len(samples)
        bucket_stats = []
        for lo, hi in self.buckets:
            sub = [s for s in samples
                   if s.get("tss_final") is not None
                   and lo <= float(s["tss_final"]) < hi]
            wins = sum(1 for s in sub if s["win"])
            wl, wh = wilson_interval(wins, len(sub))
            bucket_stats.append({
                "bucket": f"{lo}-{hi if hi <= 10 else '10+'}".replace("-10.01", "+"),
                "lo": lo, "hi": hi, "n": len(sub), "wins": wins,
                "win_rate": round(wins / len(sub), 4) if sub else None,
                "wilson": [wl, wh],
                "avg_r": round(sum(float(s["r"]) for s in sub) / len(sub), 3) if sub else None,
            })

        if n < self.min_samples:
            # 样本门槛：只输出区间表述，固定披露积累进度，不产出校准曲线
            return {
                "status": "accumulating", "n": n, "min_samples": self.min_samples,
                "disclosure": f"校准样本积累中（{n}/{self.min_samples}）——"
                              "以下为区间表述（Wilson 95%），不产出校准曲线，不作任何结论",
                "buckets": bucket_stats, "curve": None, "monotonic": None,
            }

        # ≥ 门槛：输出校准曲线点（桶中位数分数 → 实际胜率/期望R）与单调性
        curve = [{
            "bucket": b["bucket"], "n": b["n"],
            "win_rate": b["win_rate"], "avg_r": b["avg_r"],
        } for b in bucket_stats]
        rates = [b["win_rate"] for b in bucket_stats
                 if b["n"] > 0 and b["win_rate"] is not None]
        monotonic = bool(rates) and all(b >= a - 1e-9 for a, b in zip(rates, rates[1:]))
        return {
            "status": "calibrated", "n": n, "min_samples": self.min_samples,
            "disclosure": f"校准样本 {n} 条（≥{self.min_samples}），输出校准曲线点；"
                          f"分数-结果单调性：{'成立' if monotonic else '不成立（需复盘）'}",
            "buckets": bucket_stats, "curve": curve, "monotonic": monotonic,
        }
