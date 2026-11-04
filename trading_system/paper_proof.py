"""Verify paper executions from admitted observations without reading live providers."""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

from .agent_evidence import (EvidenceInvalid, normalize, read_bytes, read_json,
                             read_snapshot, strict_json)
from .calibration import CalibrationLayer
from .paper_ledger import CONTRACT, PaperObservations, project_journal
from .redline.execution import PUBLIC_PROVIDER_NAMES, StrictObservationError, market_observation
from .simulator import SimEngine


def _frame(value, day):
    if value is None:
        return None
    import pandas as pd

    if value["__type__"] != "dataframe":
        raise ValueError("Paper quote is not a table")
    labels = value["index"]
    if not labels:
        return None
    if not all(label["__type__"] == "Timestamp" for label in labels):
        raise ValueError("Paper quote has non-session dates")
    frame = pd.DataFrame(
        [[float("nan") if isinstance(item, dict) and item.get("__type__") == "missing" else item
          for item in row] for row in value["values"]],
        columns=value["columns"], index=pd.DatetimeIndex([label["value"] for label in labels]),
    )
    frame = market_observation(frame, run_date=day, market_timezone="America/New_York")
    if (not frame.index.is_unique or not frame.index.is_monotonic_increasing
            or not frame.index.equals(frame.index.normalize()) or not frame.columns.is_unique
            or not {"Open", "High", "Low", "Close"}.issubset(frame.columns)):
        raise ValueError("Paper quote dates or columns are ambiguous")
    return frame


def _recorded_histories(output, read, day):
    """Bind each chosen history to the first usable response in the observed fallback."""
    from .pipeline import _missing_data, _series_stale

    histories = {}
    native_calls = {}
    attempted = set()
    chosen = {}
    for call in output["io_calls"]:
        method = call["method"]
        if method == "paper.market_history":
            requested = read(call["input_ref"], "input")
            ticker = requested["ticker"]
            if ticker in chosen or requested != {"ticker": ticker, "days": 400}:
                raise ValueError("Paper history request is not unique")
            chosen[ticker] = read(call["output_ref"], "output")
        elif method.startswith("provider.") and method.endswith(".ohlcv"):
            requested = read(call["input_ref"], "input")
            if (len(requested["args"]) != 1 or requested["method"] != "ohlcv"
                    or requested["kwargs"] != {"days": 400}
                    or requested["provider"] not in PUBLIC_PROVIDER_NAMES
                    or method != f"provider.{requested['provider']}.ohlcv"):
                raise ValueError("Paper provider request differs from fallback contract")
            attempted.add(requested["args"][0])
            if "output_ref" in call:
                response = read(call["output_ref"], "output")
                native_calls.setdefault(requested["args"][0], []).append(response)
    if attempted != chosen.keys():
        raise ValueError("Paper history request and provider attempts differ")
    for ticker, observed in chosen.items():
        candidates = (_frame(value, day) for value in native_calls.get(ticker, []))
        selected = next((frame for frame in candidates
                         if not _missing_data(frame) and not _series_stale(frame, as_of_date=day)), None)
        projected = _frame(observed, day)
        if (projected is None) != (selected is None) or (
                projected is not None and not projected.equals(selected)):
            raise ValueError("Selected paper history differs from recorded provider response")
        histories[ticker] = projected
    return histories


def verify_paper_execution(run, admission, result, sim):
    """Re-execute the deterministic paper step and reconcile both projections."""
    try:
        clock = datetime.fromisoformat(admission["clock"]["admitted_at_utc"])
        session = admission["clock"]["run_date"]
        day = datetime.fromisoformat(session).date()
        stages = {row["stage"]: row for line in read_bytes(run, "events.jsonl").splitlines()
                  if (row := strict_json(line))["kind"] == "end"}

        def read(stage, ref, role):
            return read_snapshot(run, ref, run_id=run.name, stage=stage, role=role)

        def output(stage):
            return read(stage, stages[stage]["output_ref"], "output")

        step = "paper.step"
        step_in = read(step, stages[step]["input_ref"], "input")
        step_out = output(step)
        expected_refs = {"market_ref": stages["data.prepare"]["output_ref"],
                         "approval_ref": stages["layer4.risk"]["output_ref"],
                         "approval_input_ref": stages["layer4.risk"]["input_ref"],
                         "decision_ref": stages["decision.debate"]["output_ref"],
                         "account_version_before": (admission["parent"] or {}).get("version_sha256")}
        paper_path = run / "account-before" / "sim_portfolio.json"
        previous = read_json(run, "account-before/sim_portfolio.json")
        if (step_in["references"] != expected_refs or step_in["state"] != previous
                or step_in["run_date"] != {"__type__": "date", "value": session}):
            raise ValueError("Paper step is not bound to the admitted account and research")
        typed_result = step_in["result"]["fields"]
        risk = output("layer4.risk")["value"]
        if any(typed_result[key] != result[key] for key in ("trade_date", "provider", "action")):
            raise ValueError("Paper signal decision differs from final research")
        picks = [item["fields"] for item in typed_result["picks"]]
        if (picks != result["picks"]
                or any(item not in [entry["fields"] for entry in risk["picks"]] for item in picks)
                or typed_result["raw"]["entry_budget"] != risk["entry_budget"]
                or any(typed_result["raw"].get(key) != result["raw"].get(key)
                       for key in ("security_roster", "security_identities"))
                or any(typed_result["raw"]["pick_rationale"][pick["ticker"]] != risk["pick_rationale"][pick["ticker"]]
                       for pick in picks)):
            raise ValueError("Paper signal differs from the final model and numeric approval")
        mrs = typed_result["mrs"]
        # The same normal engine runs over sealed bytes, without creating or
        # mutating a temporary ledger and without calling a vendor.
        books = SimEngine(str(paper_path))
        histories = _recorded_histories(step_out, lambda ref, role: read(step, ref, role), day)
        observations = PaperObservations(clock, lambda ticker: histories[ticker])
        risk_in = read("layer4.risk", expected_refs["approval_input_ref"], "input")
        observations.earnings_dates = risk_in["calendar"]["earnings"]
        calendar_overlay = read_json(run, "work/events_calendar.json")
        if observations.earnings_dates != {ticker.upper(): value for ticker, value
                                            in calendar_overlay.get("earnings", {}).items()}:
            raise ValueError("Earnings evidence differs from the frozen calendar overlay")
        paper_result = SimpleNamespace(trade_date=session, provider=typed_result["provider"],
            action=typed_result["action"], picks=[SimpleNamespace(**pick) for pick in picks],
            mrs=SimpleNamespace(**mrs["fields"]) if mrs else None, raw=typed_result["raw"])
        transition = books.step(session, paper_result, None, paper=observations, run_id=run.name,
                                references=expected_refs, persist=False)
        expected = {"state": books.state, "transition": transition, "bars": observations.selected}
        if (set(observations.frames) != set(histories)
                or step_out["value"] != normalize(expected) or sim != books.state
                or output("paper.persist")["value"]["state"] != sim):
            raise ValueError("Paper state, observations or transition differ on replay")
        journal = project_journal(read_json(run, "account-before/journal.json"), sim)
        if (journal != read_json(run, "work/reports/journal.json")
                or output("journal.log")["value"]["records"] != journal
                or output("journal.settle")["value"]["records"] != journal):
            raise ValueError("Journal is not the projection of paper execution")
        calibration = CalibrationLayer()
        samples = calibration.collect_samples(journal, paper_only=True)
        if (samples != read_json(run, "work/reports/calibration_samples.json")
                or result["raw"]["calibration"] != {
                    **calibration.summarize(samples), "basis": "after_paper_execution"}):
            raise ValueError("Calibration does not use authoritative settlements")
        disclosure = {"contract": CONTRACT, "events": transition["events"],
                      "observation_status": transition["observation_status"]}
        if result["raw"]["accounting"]["paper_execution"] != disclosure:
            raise ValueError("Final accounting disclosure differs from verified paper facts")
        return disclosure
    except EvidenceInvalid:
        raise
    except (ArithmeticError, AttributeError, IndexError, KeyError, OSError, StrictObservationError,
            TypeError, ValueError) as exc:
        raise EvidenceInvalid("RESULT_INVALID", "Frozen paper-account transition cannot be reproduced",
                              stage="paper.step") from exc
