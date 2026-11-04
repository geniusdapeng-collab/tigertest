"""诸葛（复盘主持）— 编排日/周/月三个频率，产出复盘纪要并进治理事件。

频率编排（UPGRADE_PLAN_v3 §5 复盘闭环）：
  日度  归因 + 违规六条标记 + 校准层状态 → reports/复盘_<日期>.md
  周度  统计体检（三档结论）→ 并入当周末的复盘纪要
  月度  WFA 提案摘要 + 待审批提示（提案本体见 monthly.py）

审批流（三手势对齐 WorkLoom review-console）：
  approve  提案状态 → approved，写 tuned_params.json（effective_from=次日），
           复用 --use-tuned 纪律：生效动作写日报披露 + 治理事件；
  reject   必须填原因（驳回样本回流组织记忆），状态 → rejected；
  模拟盘阶段审批不阻塞 pipeline 运行（只影响参数提案生效）。

隔离性：本模块只读会计账（journal/校准样本/提案），产出只进报告层与
治理事件，绝不进入决策输入（D5 零基线）。
"""

from __future__ import annotations

import base64
import json
import logging
import os
import tempfile
import time
from contextlib import ExitStack, contextmanager
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path

from .. import config
from ..ledger_io import write_json_atomic, write_text_atomic, read_json_strict
from ..redline.execution import EvidenceWriteError
from . import monthly, weekly
from .attribution import VIOLATIONS, attribute_journal
from .monthly import (
    Proposal, _approval_lock, _load_proposal_unlocked, _save_proposal_unlocked,
    list_proposals,
)

logger = logging.getLogger(__name__)


def _read_bytes(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _restore_bytes(path: Path, value: bytes | None) -> None:
    """Restore the exact previous bytes, including non-JSON tuned files."""
    if value is None:
        path.unlink(missing_ok=True)
        return
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=path.parent, prefix=f".{path.name}.",
            suffix=".tmp", delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _recover_approval(journal: Path, transaction: dict) -> bool:
    """Under approval locks, refuse any observed bytes outside the transaction."""
    states = []
    for item in transaction["files"]:
        path = Path(item["path"])
        before = (base64.b64decode(item["before"], validate=True)
                  if item["before"] is not None else None)
        after = base64.b64decode(item["after"], validate=True)
        current = _read_bytes(path)
        if current not in (before, after):
            raise RuntimeError(f"审批恢复发现外部修改，保留事务记录: {path}")
        states.append((path, before, after, current))
    committed = all(current == after for _, _, after, current in states)
    if committed:
        logger.info("审批事务两文件已提交，恢复收尾: %s", journal)
    else:
        for path, before, _, current in states:
            if current != before:
                _restore_bytes(path, before)
        logger.warning("审批事务未完成，已恢复原文件: %s", journal)
    journal.unlink()
    return committed


@contextmanager
def _approval_guard(proposals_dir: str, tuned_path: str | None = None):
    """Recover an interrupted approval before any subsequent approval/rejection.

    The journal protects crash recovery, not simultaneous visibility of two
    ordinary files. Proposal readers fail closed while it exists. Other tuned
    writers (e.g. the separate WFA command) do not participate in these locks;
    conflicting bytes stop recovery rather than being silently overwritten.
    """
    root = Path(proposals_dir).resolve()
    journal = root / monthly.APPROVAL_TRANSACTION
    with _approval_lock(root / ".approval.lock"), ExitStack() as stack:
        raw = _read_bytes(journal)
        transaction = json.loads(raw) if raw is not None else None
        targets = {Path(tuned_path).resolve()} if tuned_path is not None else set()
        if transaction is not None:
            targets.add(Path(transaction["files"][1]["path"]))
        for target in sorted(targets):
            if target in (journal, root / ".approval.lock"):
                raise ValueError("调优文件与审批事务文件重合")
            stack.enter_context(_approval_lock(
                target.with_name(f".{target.name}.approval.lock")))
        recovered = None
        if transaction is not None and _recover_approval(journal, transaction):
            recovered = transaction
        yield journal, recovered


def _commit_approval(proposal_path: Path, proposal: Proposal,
                     tuned_path: Path, blob: dict, journal: Path) -> None:
    """Journal old bytes, publish complete JSON files, compensate on failure.

    The proposal is published first so tuned consumers never see a new file
    before its approval record exists. The unfinished journal makes that
    intermediate proposal unavailable to review readers. File flushes and
    atomic replacements do not promise a multi-file or power-loss transaction.
    """
    files = []
    for path, value in ((proposal_path, asdict(proposal)), (tuned_path, blob)):
        before = _read_bytes(path)
        # Match write_json_atomic's text-mode newline translation on Windows.
        after = json.dumps(value, ensure_ascii=False, indent=2,
                           allow_nan=False).replace("\n", os.linesep).encode("utf-8")
        files.append({"path": str(path),
                      "before": (base64.b64encode(before).decode("ascii")
                                 if before is not None else None),
                      "after": base64.b64encode(after).decode("ascii")})
    transaction = {"proposal_id": proposal.proposal_id, "files": files}
    write_json_atomic(journal, transaction)
    try:
        write_json_atomic(proposal_path, asdict(proposal))
        write_json_atomic(tuned_path, blob)
    except Exception:
        logger.exception("审批持久化失败，开始恢复原文件: %s", proposal.proposal_id)
        try:
            _recover_approval(journal, transaction)
        except Exception as recovery_error:
            logger.exception("审批恢复失败，保留事务记录: %s", journal)
            raise RuntimeError(f"审批恢复未完成: {journal}；请修复存储后重试") from recovery_error
        raise
    journal.unlink()


def _read_journal(path: str, *, strict: bool = False) -> list[dict]:
    if strict:
        from ..journal import Journal
        try:
            read_json_strict(path)
        except FileNotFoundError:
            pass
        return Journal(path).records
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
        except Exception as exc:
            logger.warning("复盘读取 journal 失败（按空账处理）: %s", exc)
    return []


class ReviewChief:
    """复盘主持：三个频率编排 + 纪要落盘 + 治理事件 + 审批流。"""

    def __init__(self, out_dir: str = config.REPORTS_DIR,
                 journal_path: str | None = None,
                 proposals_dir: str | None = None,
                 bridge=None, *, strict: bool = False, samples_path: str | None = None,
                 execution=None):
        self.out_dir = out_dir
        self.journal_path = journal_path or os.path.join(out_dir, "journal.json")
        self.proposals_dir = proposals_dir or os.path.join(
            out_dir, "review_proposals")
        self.bridge = bridge        # GovernanceBridge（可选，治理旁路）
        self.strict = strict
        self.samples_path = samples_path or os.path.join(out_dir, "calibration_samples.json")
        self.execution = execution

    def _observe(self, method, inputs, fn):
        return self.execution.call_io(method, inputs, fn) if self.execution else fn()

    # ------------------------------------------------------------ 治理事件

    def _emit(self, action: str, object_id: str, basis: list[str],
              after: dict | None = None, rule: tuple[str, str] = ("R-T15", "pass")):
        """纪要/审批动作进治理五元事件（object=报告；旁路失败不阻塞）。"""
        if self.bridge is None:
            if self.strict:
                raise ValueError("Strict review requires a governance bridge")
            return None
        try:
            from ..governance_bridge import FiveElementEvent, RuleImpact
            ev = FiveElementEvent(
                who={"type": "agent", "id": "review.chief", "version": "v6.4"},
                context={"channel": "review", "stage": "paper"},
                object={"type": "report", "id": object_id},
                decision={"action": action, "basis": basis,
                          **({"after": after} if after else {})},
                rule_impact=[RuleImpact(rule[0], result=rule[1])],
            )
            emitted = self._observe("review.governance.emit", {"event": ev},
                                    lambda: self.bridge.emit(ev))
            if self.strict and emitted is None:
                raise RuntimeError("Strict review governance event was not written")
            return emitted
        except EvidenceWriteError:
            raise
        except Exception as exc:    # Legacy governance remains nonblocking.
            if self.strict:
                raise
            logger.warning("[复盘] 治理事件写入失败（不阻塞）: %s", exc)
            return None

    # ------------------------------------------------------------ 日度

    def daily(self, trade_date: str | None = None,
              snapshots: dict[str, dict] | None = None,
              include_weekly: bool | None = None) -> dict:
        """日度复盘：归因 + 违规标记 + 校准状态 → 复盘纪要。

        include_weekly=None 时按日历自动判定（周六/日并入周度体检）。
        返回 {memo_path, attribution, calibration, weekly?, ms}。
        """
        t0 = time.perf_counter()
        if self.strict:
            if not trade_date or (self.execution and trade_date != self.execution.run_date.isoformat()):
                raise ValueError("Strict review requires the frozen market date")
        trade_date = trade_date or datetime.now().strftime("%Y-%m-%d")
        records = self._observe("review.journal.read", {"path": self.journal_path},
                                lambda: _read_journal(self.journal_path, strict=self.strict))
        att = attribute_journal(records, snapshots)

        # 校准层状态（联动：更新样本库并取 n/50 披露；与 pipeline 同口径）
        try:
            from ..calibration import CalibrationLayer
            cal = self._observe("review.calibration.run",
                {"journal_records": records, "journal_path": self.journal_path,
                 "samples_path": self.samples_path},
                lambda: CalibrationLayer(journal_path=self.journal_path,
                                         samples_path=self.samples_path).run(
                    paper_only=bool(self.execution and self.execution.authoritative_paper)))
        except EvidenceWriteError:
            raise
        except Exception as exc:
            if self.strict:
                raise
            cal = {"status": "skipped", "reason": str(exc),
                   "n": 0, "min_samples": config.CALIBRATION_MIN_SAMPLES,
                   "disclosure": f"校准层异常（已记录）: {exc}"}

        # 周度体检：周末自动并入（或显式要求）
        if include_weekly is None:
            include_weekly = datetime.strptime(
                trade_date, "%Y-%m-%d").weekday() >= 5
        weekly_out = None
        if include_weekly:
            weekly_out = weekly.weekly_checkup(
                [row for row in records if row.get("accounting_basis") != "legacy_hypothesis"], cal)

        proposals = self._observe("review.proposals.read", {"path": self.proposals_dir},
            lambda: list_proposals(self.proposals_dir, strict=True)
            if self.strict else list_proposals(self.proposals_dir))
        pending = [p for p in proposals if p.status == "pending_review"]
        memo = self._render_daily(trade_date, records, att, cal,
                                  weekly_out, pending, proposals=proposals if self.strict else None)
        memo_path = os.path.join(
            self.out_dir, f"复盘_{trade_date.replace('-', '')}.md")
        os.makedirs(self.out_dir, exist_ok=True)
        if self.strict:
            self._observe("review.memo.write", {"path": memo_path, "text": memo},
                          lambda: write_text_atomic(memo_path, memo))
        else:
            with open(memo_path, "w", encoding="utf-8") as f:
                f.write(memo)

        governance_event = self._emit(
            action="review.daily", object_id=f"复盘_{trade_date}",
            basis=[f"归因 {len(att['attributions'])} 笔",
                   f"违规命中 {att['total_violations']} 条",
                   f"校准: {cal.get('disclosure', '')}",
                   f"纪要: {memo_path}"]
            + ([f"周度体检: {weekly_out['verdict']}"] if weekly_out else []),
            after={"memo": memo_path,
                   "violations": att["violation_counts"],
                   "rule_coverage": {code: {key: value for key, value in detail.items() if key != "evidence"}
                                     for code, detail in att["violation_coverage"].items()},
                   "calibration_n": cal.get("n", 0)},
        )
        return {"memo_path": memo_path, "attribution": att,
                "calibration": cal, "weekly": weekly_out,
                "pending": [p.proposal_id for p in pending],
                **({"governance_event": governance_event,
                    "proposals": [asdict(p) for p in proposals]} if self.strict else {}),
                "ms": round((time.perf_counter() - t0) * 1000, 1)}

    # ------------------------------------------------------------ 周度

    def weekly(self) -> dict:
        """周度体检（统计员），结论进治理事件。"""
        records = _read_journal(self.journal_path)
        out = weekly.weekly_checkup([row for row in records if row.get("accounting_basis") != "legacy_hypothesis"])
        self._emit(action="review.weekly",
                   object_id=f"周度体检_{datetime.now():%Y-%m-%d}",
                   basis=[f"结论: {out['verdict']}"] + out["reasons"],
                   after={"verdict": out["verdict"]},
                   rule=("R-T15", "pass" if out["verdict"] != "告警" else "review"))
        return out

    # ------------------------------------------------------------ 月度

    def monthly(self, wfa: dict) -> Proposal:
        """月度 WFA 提案（策略优化师）：只提案不生效，结论进治理事件。"""
        p = monthly.generate_proposal(wfa, self.proposals_dir)
        self._emit(
            action="review.monthly.proposal", object_id=p.proposal_id,
            basis=[f"verdict={p.verdict}", f"DSR={p.dsr}",
                   f"OOS期望={p.oos_expectancy}R",
                   p.reason or "统计显著，待三手势审批"],
            after={"verdict": p.verdict, "dsr": p.dsr},
            rule=("R-T15", "review" if p.verdict == "pending_review" else "pass"))
        return p

    # ------------------------------------------------------------ 审批流

    def approve(self, proposal_id: str,
                tuned_path: str = "tuned_params.json") -> Proposal:
        """批准提案：状态→approved，写 tuned_params.json（次日生效+披露）。

        纪律（D7）：生效动作不直接改 config——写 tuned_params.json 后由
        下一轮 --use-tuned 显式加载（pipeline 默认不加载）；effective_from
        为次日，apply_tuned_params 在生效日前拒绝加载（次日生效的机器执行）。
        """
        try:
            proposal_path = Path(monthly._proposal_path(
                self.proposals_dir, proposal_id)).resolve()
            target = Path(tuned_path).resolve()
            if (target == proposal_path
                    or (target.exists() and proposal_path.exists()
                        and target.samefile(proposal_path))):
                raise ValueError("调优文件与提案文件不能是同一文件")
            with _approval_guard(self.proposals_dir, tuned_path) as (journal, recovered):
                p = _load_proposal_unlocked(self.proposals_dir, proposal_id)
                if (p.status == "approved" and recovered is not None
                        and recovered["proposal_id"] == proposal_id
                        and Path(recovered["files"][1]["path"]) == target):
                    # The previous invocation committed both files but could
                    # not finish journal cleanup. A retry acknowledges that
                    # exact commit; ordinary repeated approval stays invalid.
                    blob = json.loads(base64.b64decode(recovered["files"][1]["after"]))
                    tomorrow = p.effective_from
                elif p.status != "pending_review":
                    raise ValueError(f"提案 {proposal_id} 状态为 {p.status}，"
                                     "仅 pending_review 可批准")
                else:
                    tomorrow = (datetime.now().date() + timedelta(days=1)).isoformat()
                    p.status = "approved"
                    p.approved_at = datetime.now().isoformat(timespec="seconds")
                    p.effective_from = tomorrow
                    blob = {
                        "tuned_at": p.approved_at,
                        "params": p.grid_result.get("recommended_params") or {},
                        "oos_aggregate": p.oos_aggregate,
                        "dsr": p.dsr,
                        "proposal_id": p.proposal_id,
                        "effective_from": tomorrow,
                        "disclosure": f"复盘提案 {p.proposal_id} 经审批批准，"
                                      f"自 {tomorrow} 起随 --use-tuned 显式启用（次日生效；"
                                      "默认仍不加载：零基线纪律），本披露写入日报与复盘纪要；"
                                      f"DSR={p.dsr}，OOS期望={p.oos_expectancy}R",
                    }
                    _commit_approval(proposal_path, p, target, blob, journal)
        except Exception:
            logger.exception("提案 %s 审批失败", proposal_id)
            raise

        self._emit(
            action="review.proposal.approve", object_id=p.proposal_id,
            basis=[f"批准人三手势: 采纳", blob["disclosure"]],
            after={"status": "approved", "effective_from": tomorrow,
                   "tuned_params": tuned_path})
        logger.info("提案 %s 已批准，自 %s 起随 --use-tuned 生效（已披露）",
                    p.proposal_id, tomorrow)
        return p

    def reject(self, proposal_id: str, reason: str | None = None) -> Proposal:
        """驳回提案：原因必填（驳回样本回流，对齐 WorkLoom 三手势）。"""
        if not reason or not reason.strip():
            raise ValueError("驳回必须填写原因（--reason \"...\"），"
                             "驳回样本需回流组织记忆（三手势纪律）")
        try:
            with _approval_guard(self.proposals_dir):
                p = _load_proposal_unlocked(self.proposals_dir, proposal_id)
                if p.status != "pending_review":
                    raise ValueError(f"提案 {proposal_id} 状态为 {p.status}，"
                                     "仅 pending_review 可驳回")
                p.status = "rejected"
                p.reason = reason.strip()
                p.rejected_at = datetime.now().isoformat(timespec="seconds")
                _save_proposal_unlocked(p, self.proposals_dir)
        except Exception:
            logger.exception("提案 %s 驳回持久化失败", proposal_id)
            raise
        self._emit(
            action="review.proposal.reject", object_id=p.proposal_id,
            basis=[f"驳回原因: {p.reason}"],
            after={"status": "rejected"})
        logger.info("提案 %s 已驳回: %s", p.proposal_id, p.reason)
        return p

    # ------------------------------------------------------------ 纪要渲染

    def _render_daily(self, trade_date: str, records: list[dict],
                      att: dict, cal: dict, weekly_out: dict | None,
                      pending: list[Proposal], *, proposals: list[Proposal] | None = None) -> str:
        lines = [f"# 复盘纪要 — {trade_date}（诸葛团队）", ""]
        lines.append("> 口径：只复盘流程不复盘运气（附录D）；归因只基于已落账数据"
                     "与当日快照，不重构决策；本纪要属会计账，不进入决策输入（D5）。")
        lines.append("")

        # ---- 日度归因 ----
        accounting = [a for a in att["attributions"] if a["accounting_basis"] != "legacy_hypothesis"]
        closed = [a for a in accounting if a["status"] == "closed"]
        open_ = [a for a in accounting if a["status"] in ("open", "pending")]
        lines.append("## 一、日度归因")
        lines.append("")
        lines.append(f"- 台账 {len(accounting)} 笔：已结算 {len(closed)}，"
                     f"未结算信号 {len(open_)}")
        if len(accounting) < len(att["attributions"]):
            lines.append(f"- 另存历史假设记录 {len(att['attributions']) - len(accounting)} 笔，单列展示。")
        if att["attributions"]:
            lines.append("")
            lines.append("| 信号日 | 标的 | 信号层（MRS区间/主线/链/模板） | "
                         "入场（滑点） | 出场类型 | R | 违规 |")
            lines.append("|---|---|---|---|---|---|---|")
            for a in att["attributions"]:
                sl = a["signal_layer"]
                eq = a["entry_quality"]
                r_show = a["r"] if a["r"] is not None else (
                    f"{a['r_live']}(浮)" if a.get("r_live") is not None else "—")
                lines.append(
                    f"| {a['date']} | {a['ticker']} "
                    f"| {sl['mrs_band']}/{sl['sector']}/{sl['chain']}/{sl['template']} "
                    f"| {eq['trigger']}"
                    + (f"（{eq['slippage']:+.2f}）" if eq["slippage"] is not None else "")
                    + f" | {a['exit_type']} | {r_show} "
                    f"| {'、'.join(a['violations']) or '—'} |")
        lines.append("")

        # ---- 违规六条 ----
        lines.append("## 二、违规六条检测（附录D，命中必标记）")
        lines.append("")
        for code, label in VIOLATIONS.items():
            rule = att["violation_coverage"][code]
            n = rule["violations"]
            coverage = f"已检查 {rule['checked']}/{rule['denominator']}，证据不足 {rule['insufficient_evidence']}"
            outcome = f"已查命中 **{n}** 条" if n is not None else "证据不足" if rule["denominator"] else "不适用"
            lines.append(f"- {code} {label}　{outcome}，{coverage}" + (" ⚠️" if n else ""))
        if att["flagged"]:
            lines.append("")
            lines.append("命中明细: " + "；".join(
                f"{f_['date']} {f_['ticker']} [{'、'.join(f_['labels'])}]"
                for f_ in att["flagged"]))
        elif any(rule["checked"] for rule in att["violation_coverage"].values()):
            lines.append("- 有证据的检查项未发现命中。")
        lines.append("")

        # ---- 校准状态 ----
        lines.append("## 三、校准层状态")
        lines.append("")
        lines.append(f"- {cal.get('disclosure', '校准摘要缺失')}")
        lines.append("")

        # ---- 周度体检（到期时并入）----
        if weekly_out:
            lines.append("## 四、周度统计体检")
            lines.append("")
            lines.append(f"- 结论：**{weekly_out['verdict']}**")
            for r_ in weekly_out["reasons"]:
                lines.append(f"  - {r_}")
            st = weekly_out["stats"]
            lines.append(f"- 累计 {st['closed']} 笔：胜率 {st['win_rate']:.1%}，"
                         f"期望 {st['expectancy_r']}R，总 {st['total_r']}R")
            l20, l50 = st["last20"], st["last50"]
            if l20["n"]:
                lines.append(f"- 近20笔 胜率 {l20['win_rate']:.1%}（均 {l20['avg_r']}R）"
                             + (f"；近50笔 胜率 {l50['win_rate']:.1%}" if l50["n"] else ""))
            dist = weekly_out["r_distribution"]
            lines.append("- R 分布: " + " ｜ ".join(
                f"{k} {v}" for k, v in dist.items()))
            bt = st["by_template"]
            if bt:
                lines.append("- 分模板: " + " ｜ ".join(
                    f"模板{k} {v['n']}笔/胜率{v['win_rate']:.0%}/均{v['avg_r']}R"
                    for k, v in bt.items() if v["n"]))
            lines.append("")

        # ---- 参数提案 ----
        lines.append(f"## {'五' if weekly_out else '四'}、参数提案（月度 WFA）")
        lines.append("")
        if pending:
            lines.append(f"- ⚠️ 待审批提案 **{len(pending)}** 个"
                         "（`python3 main.py --review-list` 查看，"
                         "`--review-approve/--review-reject` 审批）:")
            for p in pending:
                lines.append(f"  - {p.proposal_id}: DSR={p.dsr}，"
                             f"OOS期望={p.oos_expectancy}R，"
                             f"参数={p.grid_result.get('recommended_params')}")
        else:
            lines.append("- 无待审批提案（DSR 不显著的提案已按 D7 自动 reject，"
                         "不进入审批队列）。")
        approved = [p for p in (proposals if proposals is not None else list_proposals(self.proposals_dir))
                    if p.status == "approved"]
        for p in approved:
            lines.append(f"- 披露：提案 {p.proposal_id} 已批准，"
                         f"自 {p.effective_from} 起随 --use-tuned 生效。")
        lines.append("")
        return "\n".join(lines)
