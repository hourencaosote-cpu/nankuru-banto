import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import agent
import app as web_app
import common
import eval_ai


ROOT = Path(__file__).resolve().parents[1]
CASES = Path.home() / "Developer" / "codex_tasks" / "eval_cases"
MESSAGES = {
    "G001": "Your flight is cancelled. We can extend your stay.",
    "G002": "항공편이 결항되었습니다. 연박을 안내합니다.",
    "G003": "便が欠航になりました。延泊をご案内します。",
    "G005": "Votre vol est annulé. Nous pouvons prolonger votre séjour.",
}


def item(item_type, guest_id, nights=None, message=None):
    result = {"id": f"P-{guest_id}-{item_type}", "type": item_type, "guest_id": guest_id,
              "message_draft": MESSAGES.get(guest_id, "Please review this update.") if message is None else message,
              "reason": "Official flight notice"}
    if nights is not None:
        result["nights"] = nights
    return result


class EvalJudgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.guests = common.read_json(str(ROOT / "data" / "guests.json"), {})["guests"]

    def verdict(self, expected, after, before=None, steps=3, demo=None):
        return eval_ai.judge(expected, before or [], after,
                             [{"action": "finished"}], steps, self.guests, demo=demo)

    def test_demo_initial_and_update_pass_then_detect_required_failures(self):
        initial_expected = {"extension_nights": {"G001": 2, "G002": 1, "G003": 3},
                            "staff_note_required": ["G004"], "no_extension": ["G004", "G005"]}
        initial = [item("extension", "G001", 2), item("extension", "G002", 1),
                   item("extension", "G003", 3), item("staff_note", "G004")]
        self.assertTrue(self.verdict(initial_expected, initial, demo="initial")["passed"])
        wrong_nights = [dict(part) for part in initial]
        wrong_nights[0]["nights"] = 1
        self.assertIn("extension_nights:G001", self.verdict(initial_expected, wrong_nights,
                                                            demo="initial")["failures"])
        hanja = [dict(part) for part in initial]
        hanja[1]["message_draft"] = "漢字가 있습니다."
        self.assertIn("message_language:G002", self.verdict(initial_expected, hanja,
                                                              demo="initial")["failures"])
        amount = [dict(part) for part in initial]
        amount[0]["message_draft"] = "Your refund is USD 128.00."
        self.assertIn("message_amount:G001", self.verdict(initial_expected, amount,
                                                            demo="initial")["failures"])
        self.assertIsNone(eval_ai.AMOUNT_PATTERN.search("円滑にご案内します。"))
        self.assertIsNotNone(eval_ai.AMOUNT_PATTERN.search("返金額は144円です。"))
        self.assertIn("message_language:G005", self.verdict(
            {"extension_nights": {"G005": 1}},
            [item("extension", "G005", 1, message="Your flight is cancelled.")])["failures"])
        rumor = [dict(part) for part in initial]
        rumor[0]["reason"] = "SNS rumor says refund everyone"
        self.assertIn("social_rumor_reason:G001", self.verdict(initial_expected, rumor,
                                                                demo="initial")["failures"])
        self.assertIn("max_steps_reached", self.verdict(initial_expected, initial,
                                                         steps=agent.MAX_STEPS, demo="initial")["failures"])
        self.assertIn("missing_finished_summary", eval_ai.judge(initial_expected, [], initial, [], 2,
                                                                   self.guests, demo="initial")["failures"])
        duplicate_note = initial + [dict(initial[-1])]
        self.assertIn("duplicate_staff_note", self.verdict(initial_expected, duplicate_note,
                                                            demo="initial")["failures"])

        update_expected = {"new_extensions": 0, "cancellations": 1, "refunds": 1, "reminders": 1}
        update = [item("invoice_cancellation", "G002"), item("refund", "G003"),
                  item("payment_reminder", "G001")]
        self.assertTrue(self.verdict(update_expected, update, demo="update")["passed"])
        duplicate = update + [dict(update[0])]
        self.assertIn("demo_invoice_cancellation_guest", self.verdict(update_expected, duplicate,
                                                                       demo="update")["failures"])
        new_extension = update + [item("extension", "G005", 1)]
        self.assertIn("new_extensions_count", self.verdict(update_expected, new_extension,
                                                            demo="update")["failures"])

    def test_language_checks_allow_flight_codes_but_reject_hanja_and_kana_in_korean(self):
        korean = "DEMO-202 항공편이 결항되었습니다. PayPal 청구서를 확인해 주세요."
        expected = {"extension_nights": {"G002": 1}}
        self.assertTrue(eval_ai._language_ok("Korean", korean))
        self.assertTrue(self.verdict(expected, [item("extension", "G002", 1, message=korean)])["passed"])
        for draft in ("DEMO-202 漢字가 있습니다.", "DEMO-202 かな가 있습니다."):
            with self.subTest(draft=draft):
                self.assertFalse(eval_ai._language_ok("Korean", draft))
                self.assertIn("message_language:G002", self.verdict(
                    expected, [item("extension", "G002", 1, message=draft)])["failures"])
        self.assertTrue(eval_ai._language_ok("Japanese", "DEMO-303 便が欠航しました。"))
        self.assertTrue(eval_ai._language_ok("Chinese", "DEMO-404 航班已取消。"))

    @unittest.skipUnless(CASES.is_dir(), "External evaluation cases are not installed")
    def test_all_ten_cases_accept_matching_items_and_reject_a_deviation(self):
        cases = eval_ai.load_cases(CASES)
        self.assertEqual([case["id"] for case in cases], [f"C{number:02d}" for number in range(1, 11)])
        for case in cases:
            for phase, expected in case["expected"].items():
                with self.subTest(case=case["id"], phase=phase):
                    decisions = []
                    night_map = expected.get("extension_nights", expected.get("new_extension_nights", {}))
                    decisions.extend(item("extension", guest_id, nights)
                                     for guest_id, nights in night_map.items())
                    rule = expected.get("extensions")
                    if rule and rule["count"]:
                        decisions.extend(item("extension", guest_id, rule.get("nights", 1))
                                         for guest_id in rule.get("from", [])[:rule["count"]])
                    note_guests = list(expected.get("staff_note_required", []))
                    if expected.get("leftover_direct_guest"):
                        note_guests.extend(rule["from"][rule["count"]:])
                    decisions.extend(item("staff_note", guest_id) for guest_id in note_guests)
                    self.assertTrue(self.verdict(expected, decisions)["passed"])
                    changed = [dict(decision) for decision in decisions]
                    if night_map:
                        next(decision for decision in changed if decision["type"] == "extension")["nights"] += 1
                    elif rule:
                        if rule["count"]:
                            changed.remove(next(decision for decision in changed if decision["type"] == "extension"))
                        else:
                            changed.append(item("extension", "G001", 1))
                    elif expected.get("staff_note_required"):
                        changed.remove(next(decision for decision in changed if decision["type"] == "staff_note"))
                    else:
                        changed.append(item("extension", "G005", 1))
                    self.assertFalse(self.verdict(expected, changed)["passed"])

    def test_excerpt_redacts_private_values_before_truncation(self):
        message = "Contact emily@example.test about INV-EVAL-G001 with AIzaABCDEFGHIJKLMNOPQRSTUVWXYZ123456."
        excerpt = eval_ai._safe_excerpt(message)
        self.assertNotIn("emily@example.test", excerpt)
        self.assertNotIn("INV-EVAL-G001", excerpt)
        self.assertNotIn("AIza", excerpt)
        self.assertLessEqual(len(excerpt), 80)


class GateAndEvalRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.data = self.root / "data"
        self.workspace = self.root / "workspace"
        self.data.mkdir()
        self.workspace.mkdir()
        for name in ("guests.json", "flights.json", "flights_update.json"):
            shutil.copy(ROOT / "data" / name, self.data / name)
        self.patchers = [mock.patch.object(common, "DATA_DIR", str(self.data)),
                         mock.patch.object(agent, "DATA_DIR", str(self.data)),
                         mock.patch.object(web_app, "DATA_DIR", str(self.data)),
                         mock.patch("urllib.request.urlopen", side_effect=AssertionError("Network forbidden"))]
        for patcher in self.patchers:
            patcher.start()
        self.tokens = common.set_workspace_paths(str(self.workspace))
        common.write_json(common.approvals_path(), [])

    def tearDown(self):
        common.reset_workspace_paths(self.tokens)
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.temp.cleanup()

    def _set_next(self, value):
        flights = common.read_json(str(self.data / "flights.json"), {})
        flights["flights"]["DEMO-101"]["next_available_date"] = value
        common.write_json(str(self.data / "flights.json"), flights)

    def _propose(self, nights):
        return agent.tool_propose_stay_extension({"guest_id": "G001", "nights": nights,
                                                   "reason": "Official cancellation",
                                                   "message_draft": "We can extend your stay."})

    def test_g1_requires_official_next_date(self):
        self._set_next(None)
        result = self._propose(1)
        self.assertFalse(result["accepted"])
        self.assertIn("次に乗れる日が公式に出ていない", result["reasons"][0])
        self.assertEqual(web_app._gate_reason(result["reasons"][0]),
                         "No official next departure date yet, so the nights can't be set. Sent to staff.")
        self.assertEqual(common.read_json(common.approvals_path(), []), [])

    def test_g2_allows_three_and_rejects_four_even_if_ai_asks_for_three(self):
        self._set_next("2026-08-10")
        self.assertTrue(self._propose(3)["accepted"])
        common.write_json(common.approvals_path(), [])
        self._set_next("2026-08-11")
        result = self._propose(3)
        self.assertFalse(result["accepted"])
        self.assertIn("必要な泊数が4泊で、上限の3泊", result["reasons"][0])
        self.assertEqual(web_app._gate_reason(result["reasons"][0]),
                         "The guest needs more nights than the 3-night limit. Sent to staff.")

    def test_conditional_status_labels_in_log_guest_list_and_tracker(self):
        flights = common.read_json(str(self.data / "flights.json"), {})
        flights["flights"]["DEMO-101"]["status"] = "conditional"
        flights["flights"]["DEMO-202"]["status"] = "may_be_affected"
        common.write_json(str(self.data / "flights.json"), flights)
        dashboard = web_app.build_dashboard()
        self.assertEqual(dashboard["guests"][0]["flight_status_label"], "Conditional (may turn back)")
        self.assertEqual(dashboard["guests"][1]["flight_status_label"], "May be affected")
        self.assertIn("Conditional (may turn back)", dashboard["tracker_data"]["rows"][0]["flight"])
        self.assertIn("May be affected", dashboard["tracker_data"]["rows"][1]["flight"])
        self.assertEqual(web_app._log_view({"action": "tool_call", "time": "now", "actor": "ai",
                                            "detail": {"name": "get_flight_status", "args": {"flight": "DEMO-101"}}},
                                           flights["flights"], 5, {})["label"],
                         "Checked official flight DEMO-101 → Conditional (may turn back)")
        self.assertEqual(dashboard["metrics"]["cancelled"], 2)

    def test_mocked_eval_isolated_from_app_data_and_replay(self):
        watched = [ROOT / "data" / "approvals.json", ROOT / "logs" / "decision_log.jsonl",
                   ROOT / "data" / "replay" / "gemini_run.json",
                   ROOT / "data" / "replay" / "gemini_update_run.json"]
        before = {path: path.read_bytes() if path.exists() else None for path in watched}
        responses = [
            {"candidates": [{"content": {"role": "model", "parts": [
                {"functionCall": {"name": "propose_stay_extension", "args":
                    {"guest_id": guest_id, "nights": nights, "reason": "Official cancellation",
                     "message_draft": MESSAGES[guest_id]}}}
                for guest_id, nights in (("G001", 2), ("G002", 1), ("G003", 3))
            ] + [{"functionCall": {"name": "notify_staff", "args":
                  {"guest_id": "G004", "note": "OTA booking needs staff follow-up."}}}]}}]},
            {"candidates": [{"content": {"role": "model", "parts": [{"text": "Review complete."}]}}]},
        ]
        with mock.patch.object(agent, "call_gemini", side_effect=responses):
            path, report = eval_ai.evaluate(runs=1, scenario="initial", env={"GEMINI_API_KEY": "fake"},
                                             report_dir=self.root / "reports")
        self.assertTrue(path.exists())
        self.assertTrue(report["results"][0]["passed"])
        self.assertEqual(report["results"][0]["gemini_calls"], 2)
        self.assertEqual(report["results"][0]["tool_calls"], 4)
        self.assertEqual(before, {path: path.read_bytes() if path.exists() else None for path in watched})
        report_text = path.read_text(encoding="utf-8")
        self.assertNotIn("INV-EVAL", report_text)
        self.assertNotIn("GEMINI_API_KEY", report_text)

    def test_mocked_update_uses_fake_paypal_statuses_and_no_initial_run(self):
        calls = [
            {"functionCall": {"name": "propose_invoice_cancellation", "args":
                {"guest_id": "G002", "reason": "Official flight restored",
                 "message_draft": "항공편 운항이 재개되었습니다. 청구서를 취소합니다."}}},
            {"functionCall": {"name": "propose_refund", "args":
                {"guest_id": "G003", "reason": "Official flight restored",
                 "message_draft": "便が再開しました。返金をご案内します。"}}},
            {"functionCall": {"name": "propose_payment_reminder", "args":
                {"guest_id": "G001", "reason": "Invoice still unpaid",
                 "message_draft": "Please complete payment to confirm your stay."}}},
        ]
        responses = [
            {"candidates": [{"content": {"role": "model", "parts": calls}}]},
            {"candidates": [{"content": {"role": "model", "parts": [{"text": "Review complete."}]}}]},
        ]
        with mock.patch.object(agent, "call_gemini", side_effect=responses):
            _path, report = eval_ai.evaluate(runs=1, scenario="update", env={"GEMINI_API_KEY": "fake"},
                                               report_dir=self.root / "reports")
        self.assertEqual(len(report["results"]), 1)
        self.assertTrue(report["results"][0]["passed"])
        self.assertEqual(report["results"][0]["gemini_calls"], 2)
        self.assertEqual(report["results"][0]["tool_calls"], 3)

    def test_eval_continues_after_a_failed_live_attempt_without_replay(self):
        responses = [
            RuntimeError("Gemini unavailable"),
            {"candidates": [{"content": {"role": "model", "parts": [
                {"functionCall": {"name": "propose_stay_extension", "args":
                    {"guest_id": guest_id, "nights": nights, "reason": "Official cancellation",
                     "message_draft": MESSAGES[guest_id]}}}
                for guest_id, nights in (("G001", 2), ("G002", 1), ("G003", 3))
            ] + [{"functionCall": {"name": "notify_staff", "args":
                  {"guest_id": "G004", "note": "OTA booking needs staff follow-up."}}}]}}]},
            {"candidates": [{"content": {"role": "model", "parts": [{"text": "Review complete."}]}}]},
        ]
        with mock.patch.object(agent, "call_gemini", side_effect=responses), \
             mock.patch.object(agent, "replay_model", side_effect=AssertionError("Replay forbidden")):
            _path, report = eval_ai.evaluate(runs=2, scenario="initial", env={"GEMINI_API_KEY": "fake"},
                                               report_dir=self.root / "reports")
        self.assertEqual(len(report["results"]), 2)
        self.assertFalse(report["results"][0]["passed"])
        self.assertIn("run_failed:RuntimeError", report["results"][0]["failures"])
        self.assertTrue(report["results"][1]["passed"])
        self.assertEqual(report["results"][1]["gemini_calls"], 2)

    def test_interrupt_reports_completed_runs(self):
        responses = [
            {"candidates": [{"content": {"role": "model", "parts": [{"text": "Review complete."}]}}]},
            KeyboardInterrupt(),
        ]
        with mock.patch.object(agent, "call_gemini", side_effect=responses):
            path, report = eval_ai.evaluate(runs=2, scenario="initial", env={"GEMINI_API_KEY": "fake"},
                                             report_dir=self.root / "reports")
        self.assertTrue(report["interrupted"])
        self.assertEqual(len(report["results"]), 1)
        self.assertTrue(path.exists())

    @unittest.skipUnless(CASES.is_dir(), "External evaluation cases are not installed")
    def test_single_case_selection_excludes_demo_and_case_sources(self):
        response = {"candidates": [{"content": {"role": "model", "parts": [{"text": "Review complete."}]}}]}
        with mock.patch.object(agent, "call_gemini", return_value=response) as gemini:
            path, report = eval_ai.evaluate(runs=1, scenario="both", cases_dir=CASES,
                                             case_id="C03", env={"GEMINI_API_KEY": "fake"},
                                             report_dir=self.root / "reports")
        self.assertEqual(gemini.call_count, 1)
        self.assertEqual([(part["case"], part["phase"]) for part in report["results"]],
                         [("C03", "initial")])
        self.assertNotIn("real_event", path.read_text(encoding="utf-8"))
        with self.assertRaisesRegex(ValueError, "has no update phase"):
            eval_ai.evaluate(runs=1, scenario="update", cases_dir=CASES, case_id="C03",
                             env={"GEMINI_API_KEY": "fake"}, report_dir=self.root / "reports")


if __name__ == "__main__":
    unittest.main()
