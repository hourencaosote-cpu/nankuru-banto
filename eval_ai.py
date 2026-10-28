"""Evaluate live Gemini decisions in isolated fictional workspaces.

Run manually. This module never sends a PayPal request and never records replay data.
"""

import argparse
import contextlib
import io
import json
import re
import shutil
import tempfile
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from unittest import mock

import agent
import common
import paypal_tools


ROOT = Path(__file__).resolve().parent
GATE_ACTIONS = {
    "rejected_proposal", "draft_rewrite_requested", "rejected_cancellation",
    "rejected_reminder", "rejected_refund", "blocked_unknown_tool", "stopped_max_steps",
}
MESSAGE_TYPES = {"extension", "invoice_cancellation", "refund", "payment_reminder"}
ACTION_TYPES = {"cancellations": "invoice_cancellation", "refunds": "refund",
                "reminders": "payment_reminder"}
AMOUNT_PATTERN = re.compile(
    r"\$\s*\d|\d[\d,.]*\s*\$|(?:\bUSD\b|ドル|달러|美元|円)\s*\d|"
    r"\d[\d,.]*\s*(?:\bUSD\b|ドル|달러|美元|円)|\b\d+\.\d{2}\b",
    re.I,
)
PRIVATE_PATTERN = re.compile(
    r"AIza[A-Za-z0-9_-]+|[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}|\bINV(?:2)?-[A-Za-z0-9_-]+\b",
    re.I,
)
HAN = re.compile(r"[\u3400-\u4DBF\u4E00-\u9FFF]")
HANGUL = re.compile(r"[\uAC00-\uD7AF]")
KANA = re.compile(r"[\u3040-\u30FF]")
LATIN = re.compile(r"[A-Za-z]")
FRENCH_WORDS = re.compile(r"\b(?:votre|vous|nous|pour|avec|vol|séjour|chambre|prolonger|annulé|"
                          r"annulée|veuillez|pouvons|bonjour|désolé|désolée)\b", re.I)


def load_cases(folder, case_id=None):
    """Read the supplied fictional cases without copying their source articles."""
    if folder is None:
        if case_id:
            raise ValueError("--case requires --cases")
        return []
    paths = sorted(Path(folder).expanduser().glob("C[0-9][0-9].json"))
    cases = []
    for path in paths:
        if case_id and path.stem != case_id:
            continue
        case = json.loads(path.read_text(encoding="utf-8"))
        if case.get("id") != path.stem or not isinstance(case.get("expected"), dict):
            raise ValueError(f"Invalid evaluation case: {path.name}")
        cases.append(case)
    if case_id and not cases:
        raise ValueError(f"Evaluation case {case_id} was not found")
    if not paths:
        raise ValueError("No C01.json-style evaluation cases were found")
    return cases


def _seed_approvals(state, guests, discount):
    guest_by_id = {guest["id"]: guest for guest in guests}
    queue = []
    for number, raw in enumerate(state.get("approvals", []), 1):
        guest = guest_by_id[raw["guest_id"]]
        item = dict(raw)
        item.setdefault("id", f"P{number:03d}")
        item.setdefault("guest_name", guest["name"])
        item.setdefault("language", guest["language"])
        item.setdefault("created_at", "2026-08-06T22:00:00")
        if item["type"] == "extension":
            nights = int(item["nights"])
            amount = round(guest["nightly_rate_usd"] * nights * (100 - discount) / 100, 2)
            item.update(nightly_rate_usd=guest["nightly_rate_usd"], discount_percent=discount,
                        amount_usd=amount)
            item.setdefault("invoice_id", f"INV-EVAL-{guest['id']}")
        elif item["type"] == "staff_note":
            item.setdefault("note", "Staff follow-up needed.")
        queue.append(item)
    return queue


def _demo_update_state():
    return {"approvals": [
        {"type": "extension", "guest_id": "G001", "nights": 2, "status": "sent"},
        {"type": "extension", "guest_id": "G002", "nights": 1, "status": "sent"},
        {"type": "extension", "guest_id": "G003", "nights": 3, "status": "confirmed"},
        {"type": "staff_note", "guest_id": "G004", "status": "pending"},
    ], "paypal_status": {"G001": "SENT", "G002": "SENT", "G003": "PAID"}}


def _safe_excerpt(message, secrets=()):
    safe = str(message)
    for secret in sorted((str(value) for value in secrets if value), key=len, reverse=True):
        safe = safe.replace(secret, "[redacted]")
    return PRIVATE_PATTERN.sub("[redacted]", safe)[:80]


def _language_ok(language, message):
    if language == "Korean":
        return bool(HANGUL.search(message)) and not (HAN.search(message) or KANA.search(message))
    if language == "Japanese":
        return bool(KANA.search(message)) and not HANGUL.search(message)
    if language == "French":
        return (bool(LATIN.search(message)) and bool(FRENCH_WORDS.search(message))
                and not (HAN.search(message) or KANA.search(message) or HANGUL.search(message)))
    if language == "English":
        return bool(LATIN.search(message)) and not (HAN.search(message) or KANA.search(message)
                                                   or HANGUL.search(message))
    if language == "Chinese":
        return bool(HAN.search(message)) and not (KANA.search(message) or HANGUL.search(message))
    return False


def _gate_reasons(entries):
    reasons = Counter()
    rejected = 0
    for entry in entries:
        action = entry.get("action")
        if action not in GATE_ACTIONS:
            continue
        rejected += 1
        detail = entry.get("detail") or {}
        values = detail.get("reasons") or [detail.get("reason") or action]
        for value in values:
            # The report stores a category, never tool arguments or arbitrary model text.
            if "次に乗れる日が公式に出ていない" in value:
                category = "no_official_next_date"
            elif "必要な泊数が" in value and "上限" in value:
                category = "over_three_nights"
            elif "漢字が含まれ" in value:
                category = "korean_hanja"
            elif "OTA経由" in value:
                category = "ota_booking"
            elif "空室がもうありません" in value:
                category = "no_rooms"
            elif "欠航ではありません" in value:
                category = "not_cancelled"
            elif "すでに" in value or "重複" in value:
                category = "duplicate_or_existing"
            elif "案内文" in value:
                category = "missing_message"
            elif "必要なのは" in value:
                category = "too_many_nights"
            else:
                category = action
            reasons[category] += 1
    return rejected, dict(reasons)


def judge(expected, before, after, entries, steps, guests, *, demo=None, secrets=()):
    """Check one phase using only its new decisions and its declared expectations."""
    new = after[len(before):]
    failures = []
    warnings = []
    messages = []
    guest_by_id = {guest["id"]: guest for guest in guests}
    extensions = [item for item in new if item.get("type") == "extension"]
    notes = [item for item in new if item.get("type") == "staff_note"]
    if steps >= agent.MAX_STEPS or any(e.get("action") == "stopped_max_steps" for e in entries):
        failures.append("max_steps_reached")
    if not any(e.get("action") == "finished" for e in entries):
        failures.append("missing_finished_summary")

    for key in ("extension_nights", "new_extension_nights"):
        for guest_id, nights in expected.get(key, {}).items():
            matches = [item for item in extensions if item.get("guest_id") == guest_id]
            if len(matches) != 1 or matches[0].get("nights") != nights:
                failures.append(f"{key}:{guest_id}")
    for guest_id in expected.get("no_extension", []):
        if any(item.get("guest_id") == guest_id for item in extensions):
            failures.append(f"unexpected_extension:{guest_id}")
    if "new_extensions" in expected and len(extensions) != expected["new_extensions"]:
        failures.append("new_extensions_count")

    ext_rule = expected.get("extensions")
    eligible = set(ext_rule.get("from", [])) if ext_rule and ext_rule.get("from") else None
    selected = [item for item in extensions if eligible is None or item.get("guest_id") in eligible]
    if ext_rule:
        if len(selected) != ext_rule["count"]:
            failures.append("extensions_count")
        if "nights" in ext_rule and any(item.get("nights") != ext_rule["nights"] for item in selected):
            failures.append("extensions_nights")

    expected_notes = set(expected.get("staff_note_required", []))
    expected_notes.update(expected.get("staff_note_optional", []))
    if expected.get("leftover_direct_guest"):
        leftovers = eligible - {item.get("guest_id") for item in selected} if eligible else set()
        if len(leftovers) != 1:
            failures.append("leftover_direct_guest_count")
        expected_notes.update(leftovers)
    for guest_id in expected_notes - set(expected.get("staff_note_optional", [])):
        if not any(item.get("guest_id") == guest_id for item in notes):
            failures.append(f"missing_staff_note:{guest_id}")
    for guest_id in {item.get("guest_id") for item in notes} - expected_notes:
        warnings.append(f"unexpected_staff_note:{guest_id}")
    all_notes = [item.get("guest_id") for item in after if item.get("type") == "staff_note"]
    if len(all_notes) != len(set(all_notes)):
        failures.append("duplicate_staff_note")

    for key, item_type in ACTION_TYPES.items():
        if key in expected and sum(item.get("type") == item_type for item in new) != expected[key]:
            failures.append(f"{key}_count")
    if demo == "initial":
        if len(extensions) != 3 or {item.get("guest_id") for item in extensions} != {"G001", "G002", "G003"}:
            failures.append("demo_extension_guests")
        if len(notes) != 1 or notes[0].get("guest_id") != "G004":
            failures.append("demo_staff_note_guests")
    elif demo == "update":
        expected_actions = {"invoice_cancellation": "G002", "refund": "G003",
                            "payment_reminder": "G001"}
        for item_type, guest_id in expected_actions.items():
            matches = [item for item in new if item.get("type") == item_type]
            if len(matches) != 1 or matches[0].get("guest_id") != guest_id:
                failures.append(f"demo_{item_type}_guest")
        if extensions or notes:
            failures.append("demo_unexpected_new_proposal")

    for item in new:
        if item.get("type") not in MESSAGE_TYPES:
            continue
        guest_id = item.get("guest_id")
        message = str(item.get("message_draft") or "")
        language = guest_by_id.get(guest_id, {}).get("language")
        messages.append({"guest_id": guest_id, "type": item["type"],
                         "excerpt": _safe_excerpt(message, secrets)})
        if not message.strip() or not _language_ok(language, message):
            failures.append(f"message_language:{guest_id}")
        if AMOUNT_PATTERN.search(message):
            failures.append(f"message_amount:{guest_id}")
        if item["type"] == "extension" and re.search(r"refund|返金|rumou?r|SNS|デマ|噂", str(item.get("reason") or ""), re.I):
            failures.append(f"social_rumor_reason:{guest_id}")

    return {"passed": not failures, "failures": sorted(set(failures)),
            "warnings": sorted(set(warnings)), "message_excerpts": messages}


def _read_log(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _run_phase(label, phase, run_number, expected, guests, paypal_status, env, *, demo=None):
    before = common.read_json(common.approvals_path(), [])
    log_path = Path(common.log_path())
    before_log = len(_read_log(log_path))
    calls = 0
    http_attempts = 0
    tool_calls = 0
    original_call = agent.call_gemini
    original_tool = agent.run_tool

    def counted_gemini(model_env, contents):
        nonlocal calls, http_attempts
        calls += 1
        try:
            return original_call(model_env, contents)
        finally:
            state = agent._run_state.get()
            if state is not None:
                http_attempts = state["http_calls"]

    def counted_tool(name, args):
        nonlocal tool_calls
        tool_calls += 1
        return original_tool(name, args)

    invoice_guests = {item.get("invoice_id"): item.get("guest_id") for item in before
                      if item.get("type") == "extension" and item.get("invoice_id")}

    def fake_read_invoice(_env, invoice_id):
        guest_id = invoice_guests.get(invoice_id)
        if guest_id is None:
            raise RuntimeError("Unknown evaluation invoice")
        status = paypal_status.get(guest_id, "SENT")
        return {"status": status, "due_amount": None, "viewed_by_recipient": False,
                "last_sent_time": None}

    error_type = None
    start = time.perf_counter()
    with mock.patch.object(agent, "call_gemini", counted_gemini), \
         mock.patch.object(agent, "run_tool", counted_tool), \
         mock.patch.object(agent, "read_invoice", fake_read_invoice), \
         mock.patch.object(paypal_tools, "read_invoice", fake_read_invoice), \
         mock.patch.object(paypal_tools, "_call", side_effect=AssertionError("PayPal disabled in evaluation")), \
         contextlib.redirect_stdout(io.StringIO()):
        try:
            agent.run_ai(env, mode="live", web=True)
        except (Exception, SystemExit) as error:
            error_type = type(error).__name__
    seconds = round(time.perf_counter() - start, 3)
    after = common.read_json(common.approvals_path(), [])
    entries = _read_log(log_path)[before_log:]
    secrets = [value for key, value in env.items()
               if any(marker in key for marker in ("KEY", "SECRET", "TOKEN", "CLIENT_ID", "BUYER_EMAIL"))
               and isinstance(value, str) and len(value) >= 4]
    verdict = judge(expected, before, after, entries, calls, guests, demo=demo, secrets=secrets)
    if error_type:
        verdict["failures"] = sorted(set(verdict["failures"] + [f"run_failed:{error_type}"]))
        verdict["passed"] = False
    rejected, reasons = _gate_reasons(entries)
    return {"case": label, "phase": phase, "run": run_number, **verdict,
            "gemini_calls": http_attempts or calls, "steps": calls, "tool_calls": tool_calls,
            "seconds": seconds, "gate_rejections": rejected,
            "gate_reasons": reasons}


def _run_isolated(label, run_number, initial, update, state, expected, phases, env, *, demo=False,
                  on_result=None):
    results = []
    with tempfile.TemporaryDirectory(prefix="nankuru-eval-") as temporary:
        data = Path(temporary) / "data"
        workspace = Path(temporary) / "workspace"
        data.mkdir()
        workspace.mkdir()
        shutil.copy(ROOT / "data" / "guests.json", data / "guests.json")
        common.write_json(str(data / "flights.json"), initial)
        if update is not None:
            common.write_json(str(data / "flights_update.json"), update)
        guest_data = common.read_json(str(data / "guests.json"), {})
        guests = guest_data["guests"]
        discount = guest_data["hotel_policy"]["typhoon_discount_percent"]
        seed = _seed_approvals(state, guests, discount)
        common.write_json(str(workspace / "approvals.json"), seed)
        common.write_json(str(workspace / "scenario.json"), {"notice": "initial"})
        with mock.patch.object(common, "DATA_DIR", str(data)), mock.patch.object(agent, "DATA_DIR", str(data)):
            tokens = common.set_workspace_paths(str(workspace))
            try:
                if "initial" in phases or (update is not None and not demo):
                    result = _run_phase(label, "initial", run_number, expected.get("initial", {}),
                                        guests, state.get("paypal_status", {}), env,
                                        demo="initial" if demo else None)
                    if "initial" in phases:
                        results.append(result)
                        if on_result:
                            on_result(result)
                if update is not None and "update" in phases:
                    common.write_json(str(workspace / "scenario.json"), {"notice": "update"})
                    result = _run_phase(label, "update", run_number, expected.get("update", {}),
                                        guests, state.get("paypal_status", {}), env,
                                        demo="update" if demo else None)
                    results.append(result)
                    if on_result:
                        on_result(result)
            finally:
                common.reset_workspace_paths(tokens)
    return results


def _summarize(results):
    by_phase = defaultdict(list)
    by_case_run = defaultdict(list)
    failures = Counter()
    gates = Counter()
    for result in results:
        by_phase[f"{result['case']}/{result['phase']}"].append(result)
        by_case_run[(result["case"], result["run"])].append(result["passed"])
        failures.update(result["failures"])
        gates.update(result["gate_reasons"])
    phase_summary = {}
    for label, items in sorted(by_phase.items()):
        elapsed = [item["seconds"] for item in items]
        phase_summary[label] = {"runs": len(items), "passed": sum(item["passed"] for item in items),
                                "pass_rate": round(sum(item["passed"] for item in items) / len(items), 3),
                                "average_seconds": round(sum(elapsed) / len(elapsed), 3),
                                "max_seconds": max(elapsed)}
    case_summary = {}
    for case in sorted({name for name, _ in by_case_run}):
        run_passes = [all(values) for (name, _), values in by_case_run.items() if name == case]
        case_summary[case] = {"runs": len(run_passes), "passed": sum(run_passes),
                              "pass_rate": round(sum(run_passes) / len(run_passes), 3)}
    return {"phases": phase_summary, "cases": case_summary,
            "top_failures": failures.most_common(10), "gate_reasons": dict(gates)}


def evaluate(*, runs=3, scenario="both", cases_dir=None, case_id=None, env=None, report_dir=None):
    """Run live evaluation. Tests inject a fake Gemini and a temporary report directory."""
    if runs < 1:
        raise ValueError("--runs must be at least 1")
    if scenario not in {"initial", "update", "both"}:
        raise ValueError("--scenario must be initial, update, or both")
    cases = load_cases(cases_dir, case_id)
    env = common.load_env() if env is None else env
    report_dir = Path(report_dir) if report_dir is not None else ROOT / "eval_reports"
    phases = {"initial", "update"} if scenario == "both" else {scenario}
    if case_id and not any(phases & ({"initial", "update"} if case.get("update") is not None
                                  else {"initial"}) for case in cases):
        raise ValueError(f"Evaluation case {case_id} has no {scenario} phase")
    demo_initial = common.read_json(str(ROOT / "data" / "flights.json"), {})
    demo_update = common.read_json(str(ROOT / "data" / "flights_update.json"), {})
    demo_expected = {
        "initial": {"extension_nights": {"G001": 2, "G002": 1, "G003": 3},
                    "no_extension": ["G004", "G005"], "staff_note_required": ["G004"]},
        "update": {"new_extensions": 0, "cancellations": 1, "refunds": 1, "reminders": 1},
    }
    results = []
    interrupted = False

    def collect(item):
        results.append(item)
        _print_result(item)

    try:
        for number in range(1, runs + 1):
            if not case_id:
                if "initial" in phases:
                    _run_isolated("demo", number, demo_initial, None, {}, demo_expected,
                                  {"initial"}, env, demo=True, on_result=collect)
                if "update" in phases:
                    _run_isolated("demo", number, demo_initial, demo_update,
                                  _demo_update_state(), demo_expected, {"update"}, env,
                                  demo=True, on_result=collect)
            for case in cases:
                available_phases = {"initial", "update"} if case.get("update") is not None else {"initial"}
                case_phases = phases & available_phases
                if not case_phases:
                    continue
                _run_isolated(case["id"], number, case["initial"], case.get("update"),
                              case.get("state", {}), case["expected"], case_phases, env,
                              on_result=collect)
    except KeyboardInterrupt:
        interrupted = True
        print("Interrupted; reporting completed runs.")

    summary = _summarize(results)
    for name, item in summary["phases"].items():
        print(f"{name}: pass {item['passed']}/{item['runs']} ({item['pass_rate']:.0%}), "
              f"average {item['average_seconds']:.2f}s, max {item['max_seconds']:.2f}s")
    if cases:
        print("Case pass rates:", ", ".join(f"{key} {value['pass_rate']:.0%}"
                                           for key, value in summary["cases"].items() if key != "demo"))
        print("Top failures:", summary["top_failures"])
        print("Gate reasons:", summary["gate_reasons"])
    report = {"created_at": datetime.now().isoformat(timespec="seconds"),
              "interrupted": interrupted, "results": results, "summary": summary}
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / f"{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.json"
    common.write_json(str(report_path), report)
    print(f"Report: {report_path}")
    return report_path, report


def _print_result(item):
    status = "合格" if item["passed"] else "不合格"
    failures = ",".join(item["failures"]) if item["failures"] else "-"
    print(f"{item['case']}/{item['phase']} #{item['run']}: {status} ({failures}) "
          f"Gemini {item['gemini_calls']}回, 道具 {item['tool_calls']}回, "
          f"{item['seconds']:.2f}秒, gate却下 {item['gate_rejections']}件")


def main():
    parser = argparse.ArgumentParser(description="Evaluate live Gemini decisions without PayPal or replay writes")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--scenario", choices=("initial", "update", "both"), default="both")
    parser.add_argument("--cases", type=Path)
    parser.add_argument("--case")
    args = parser.parse_args()
    try:
        evaluate(runs=args.runs, scenario=args.scenario, cases_dir=args.cases, case_id=args.case)
    except ValueError as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
