import json
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import agent
import app as web_app
import approve
import check_payments
import common


ROOT = Path(__file__).resolve().parents[1]


class AppSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.data_dir = self.root / "data"
        self.log_dir = self.root / "logs"
        self.data_dir.mkdir()
        self.log_dir.mkdir()
        shutil.copy(ROOT / "data" / "guests.json", self.data_dir / "guests.json")
        shutil.copy(ROOT / "data" / "flights.json", self.data_dir / "flights.json")
        self.workspace_id = "a" * 32
        self.workspace_dir = self.data_dir / "workspaces" / self.workspace_id
        self.workspace_dir.mkdir(parents=True)
        self.approvals_path = self.workspace_dir / "approvals.json"
        self.log_path = self.workspace_dir / "decision_log.jsonl"
        self._write_approvals([])
        self.log_path.write_text("", encoding="utf-8")

        replacements = (
            (agent, "DATA_DIR", str(self.data_dir)),
            (common, "DATA_DIR", str(self.data_dir)),
            (common, "LOG_DIR", str(self.log_dir)),
            (web_app, "DATA_DIR", str(self.data_dir)),
        )
        self.patchers = [mock.patch.object(module, name, value) for module, name, value in replacements]
        self.patchers.extend(
            [
                mock.patch("urllib.request.urlopen", side_effect=AssertionError("Network access is forbidden in tests")),
                mock.patch.object(web_app, "load_env", side_effect=AssertionError("env.txt must not be read in tests")),
                mock.patch.object(approve, "load_env", side_effect=AssertionError("env.txt must not be read in tests")),
                mock.patch.object(check_payments, "load_env", side_effect=AssertionError("env.txt must not be read in tests")),
            ]
        )
        for patcher in self.patchers:
            patcher.start()
        self.path_tokens = common.set_workspace_paths(str(self.workspace_dir))

        web_app.app.config.update(TESTING=True, SECRET_KEY="test-only-secret")
        self.client = web_app.app.test_client()
        with self.client.session_transaction() as session_data:
            session_data["workspace_id"] = self.workspace_id

    def tearDown(self):
        common.reset_workspace_paths(self.path_tokens)
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.temp_dir.cleanup()

    def _write_approvals(self, items):
        self.approvals_path.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")

    def _read_approvals(self):
        return json.loads(self.approvals_path.read_text(encoding="utf-8"))

    def _write_log_entries(self, entries):
        self.log_path.write_text(
            "".join(json.dumps(entry, ensure_ascii=False) + "\n" for entry in entries),
            encoding="utf-8",
        )

    def _csrf_token(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        with self.client.session_transaction() as session_data:
            return session_data["csrf_token"]

    def _post(self, path):
        return self.client.post(path, data={"csrf_token": self._csrf_token()})

    def _tracker(self):
        page = self.client.get("/").get_data(as_text=True)
        match = re.search(r'<script type="application/json" id="tracker-data">(.*?)</script>', page, re.S)
        self.assertIsNotNone(match)
        return page, json.loads(match.group(1))

    @staticmethod
    def _pending_extension(item_id="P001", guest_id="G001", guest_name="Emily Carter"):
        return {
            "id": item_id,
            "type": "extension",
            "guest_id": guest_id,
            "guest_name": guest_name,
            "language": "English",
            "nights": 2,
            "nightly_rate_usd": 80,
            "discount_percent": 20,
            "amount_usd": 128.0,
            "reason": "Official cancellation confirmed.",
            "message_draft": "A safe demo message.",
            "price_line": "System-calculated price line.",
            "evidence": "DEMO-101: cancelled / official demo notice",
            "status": "pending",
            "created_at": "2026-10-15T10:00:00",
        }

    def test_post_requires_csrf_and_approval_get_only_redirects(self):
        self.assertEqual(self.client.post("/run-ai").status_code, 400)
        self.assertEqual(self.client.get("/proposals/P001/approve").status_code, 302)

    def test_get_to_every_post_action_only_shows_wakeup_message(self):
        paths = (
            "/run-ai", "/reset-demo", "/check-payments",
            "/proposals/P001/approve", "/proposals/P001/reject", "/items/P001/later",
            "/staff-notes/P001/handle", "/proposals/P001/record-sandbox-payment",
        )
        with mock.patch.object(agent, "run_ai") as run_ai, \
             mock.patch.object(approve, "create_extension_invoice") as invoice, \
             mock.patch.object(web_app, "check_payments_action") as payments, \
             mock.patch.object(web_app, "record_invoice_payment") as record_payment, \
             mock.patch.object(web_app, "archive_demo_state") as reset:
            for path in paths:
                with self.subTest(path=path):
                    response = self.client.get(path)
                    self.assertEqual(response.status_code, 302)
                    self.assertEqual(response.headers["Location"], "/")
                    page = self.client.get("/").get_data(as_text=True)
                    self.assertIn("The server just woke up. Please press the button again.", page)
        run_ai.assert_not_called()
        invoice.assert_not_called()
        payments.assert_not_called()
        record_payment.assert_not_called()
        reset.assert_not_called()
        self.assertEqual(self._read_approvals(), [])
        self.assertEqual(self.log_path.read_text(encoding="utf-8"), "")
        self.assertEqual(self.client.put("/run-ai").status_code, 405)

    def test_unexpected_ai_failure_logs_sanitized_traceback_and_safe_diagnostic(self):
        secret = "test-private-key-value"
        with mock.patch.object(web_app, "load_env", return_value={"GEMINI_API_KEY": secret}), \
             mock.patch.object(agent, "run_ai", side_effect=RuntimeError(secret)), \
             self.assertLogs(web_app.app.logger, level="ERROR") as captured:
            self.assertEqual(self._post("/run-ai").status_code, 302)

        self.assertNotIn(secret, "\n".join(captured.output))
        self.assertIn("Traceback (most recent call last)", "\n".join(captured.output))
        page = self.client.get("/").get_data(as_text=True)
        self.assertIn("AI check failed safely<small class=\"activity-diagnostic\"> · RuntimeError", page)
        self.assertNotIn(secret, page)
        entries = [json.loads(line) for line in self.log_path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(entries[-1]["detail"], {"error_type": "RuntimeError"})

    def test_tracker_embeds_five_guests_and_community_grid_assets(self):
        page, tracker = self._tracker()

        self.assertEqual(len(tracker["rows"]), 5)
        self.assertEqual(tracker["totals"]["proposalNights"], 0)
        self.assertEqual(tracker["totals"]["amountUsd"], 0)
        self.assertTrue(all(row["status"] == "Awaiting AI review" for row in tracker["rows"]))
        self.assertIn("Guest &amp; invoice tracker", page)
        self.assertIn("ag-grid-community@32.3.3/dist/ag-grid-community.min.js", page)
        self.assertIn("/static/tracker.js", page)
        self.assertIn("Guest departures", page)

    def test_tracker_status_and_amount_follow_mocked_approval_and_payment(self):
        self._write_approvals([self._pending_extension()])
        _, tracker = self._tracker()
        row = next(row for row in tracker["rows"] if row["guest"] == "Emily Carter")
        self.assertEqual((row["status"], row["amountUsd"], row["proposalNights"]),
                         ("Pending approval", 128.0, 2))

        with mock.patch.object(approve, "load_env", return_value={"BUYER_EMAIL": "buyer@example.test"}), \
             mock.patch.object(approve, "create_extension_invoice", return_value=("INV-123456", "128.00")), \
             mock.patch.object(approve, "send_invoice", return_value=("SENT", None)):
            self.assertEqual(self._post("/proposals/P001/approve").status_code, 302)

        _, tracker = self._tracker()
        row = next(row for row in tracker["rows"] if row["guest"] == "Emily Carter")
        self.assertEqual((row["status"], row["amountUsd"], row["invoice"]),
                         ("Invoice sent", 128.0, "123456"))

        with mock.patch.object(check_payments, "load_env", return_value={"PAYPAL_CLIENT_ID": "test"}), \
             mock.patch.object(check_payments, "_get_token", return_value="test-token"), \
             mock.patch.object(check_payments, "get_invoice_status", return_value="PAID"):
            self.assertEqual(self._post("/check-payments").status_code, 302)

        _, tracker = self._tracker()
        row = next(row for row in tracker["rows"] if row["guest"] == "Emily Carter")
        self.assertEqual((row["status"], row["amountUsd"]), ("Paid · confirmed", 128.0))
        self.assertEqual(tracker["totals"]["proposalNights"], 2)
        self.assertEqual(tracker["totals"]["amountUsd"], 128.0)

    def test_tracker_json_escapes_script_end_in_guest_name(self):
        guest_data = json.loads((self.data_dir / "guests.json").read_text(encoding="utf-8"))
        guest_data["guests"][0]["name"] = "</script><script>alert(1)</script>"
        (self.data_dir / "guests.json").write_text(json.dumps(guest_data), encoding="utf-8")

        page, tracker = self._tracker()

        self.assertEqual(tracker["rows"][0]["guest"], "</script><script>alert(1)</script>")
        self.assertNotIn("</script><script>alert(1)</script>", page)
        self.assertIn("\\u003c/script\\u003e", page)

    def test_tracker_uses_guest_list_response_after_ai_check(self):
        def fake_gemini(_env, _contents):
            return {"candidates": [{"content": {"role": "model", "parts": [{"text": "Review complete."}]}}]}

        with mock.patch.object(web_app, "load_env", return_value={}), \
             mock.patch.object(agent, "call_gemini", new=fake_gemini), \
             mock.patch("builtins.print"):
            self.assertEqual(self._post("/run-ai").status_code, 302)

        _, tracker = self._tracker()
        sophie = next(row for row in tracker["rows"] if row["guest"] == "Sophie Martin")
        self.assertEqual(sophie["status"], "No action needed")
        self.assertEqual(web_app.build_dashboard()["guests"][-1]["response"], sophie["status"])

    def test_departure_labels_follow_checkout_date_and_notice_time(self):
        guest_file = self.data_dir / "guests.json"
        flight_file = self.data_dir / "flights.json"
        guest_data = json.loads(guest_file.read_text(encoding="utf-8"))
        flight_data = json.loads(flight_file.read_text(encoding="utf-8"))
        for guest in guest_data["guests"]:
            guest["checkout_date"] = "2026-08-09"
        flight_data["as_of"] = "2026-08-08 22:00"
        guest_file.write_text(json.dumps(guest_data), encoding="utf-8")
        flight_file.write_text(json.dumps(flight_data), encoding="utf-8")

        page = self.client.get("/").get_data(as_text=True)
        self.assertIn("guests departing Aug 9", page)
        self.assertIn("TOMORROW · AUG 9", page)
        self.assertNotIn("guests departing Aug 7", page)

        flight_data["as_of"] = "2026-08-09 08:00"
        flight_file.write_text(json.dumps(flight_data), encoding="utf-8")
        self.assertIn("TODAY · AUG 9", self.client.get("/").get_data(as_text=True))

    def test_model_log_stays_audited_but_only_appears_beside_completed_review(self):
        entries = [
            {"time": "2026-10-15T10:00:00", "actor": "human", "action": "deferred",
             "detail": {"id": f"item-{index}"}}
            for index in range(18)
        ]
        entries.extend([
            {"time": "2026-10-15T10:01:00", "actor": "ai", "action": "gemini_model_used",
             "detail": {"model": "gemini-3.8-flash"}},
            {"time": "2026-10-15T10:02:00", "actor": "ai", "action": "finished",
             "detail": {"summary": "Review complete."}},
        ])
        self._write_log_entries(entries)

        page = self.client.get("/").get_data(as_text=True)
        activity = web_app.build_dashboard()["activity"]
        self.assertEqual(len(activity), 18)
        self.assertEqual(activity[0]["label"], "AI review completed")
        self.assertEqual(activity[0]["model"], "gemini-3.8-flash")
        self.assertEqual(activity[-1]["subject"], "item-1")
        self.assertNotIn("Gemini model responded", page)
        self.assertIn("gemini-3.8-flash", page)
        self.assertEqual(json.loads(self.log_path.read_text(encoding="utf-8").splitlines()[-2])["action"],
                         "gemini_model_used")

    def test_two_ai_checks_do_not_duplicate_staff_note_and_log_investigation(self):
        def fake_gemini(_env, contents):
            if len(contents) == 1:
                return {
                    "candidates": [{
                        "content": {
                            "role": "model",
                            "parts": [
                                {"functionCall": {"name": "get_flight_status", "args": {"flight": "DEMO-404"}}},
                                {"functionCall": {"name": "get_social_posts", "args": {}}},
                                {"functionCall": {
                                    "name": "notify_staff",
                                    "args": {"guest_id": "G004", "note": "Manual OTA follow-up required."},
                                }},
                            ],
                        }
                    }]
                }
            return {"candidates": [{"content": {"role": "model", "parts": [{"text": "Done"}]}}]}

        with mock.patch.object(web_app, "load_env", return_value={}), \
             mock.patch.object(agent, "call_gemini", new=fake_gemini), \
             mock.patch("builtins.print"):
            self.assertEqual(self._post("/run-ai").status_code, 302)
            self.assertEqual(self._post("/run-ai").status_code, 302)

        staff_notes = [item for item in self._read_approvals() if item["type"] == "staff_note"]
        self.assertEqual(len(staff_notes), 1)
        log_entries = [json.loads(line) for line in self.log_path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(sum(entry["action"] == "duplicate_staff_note" for entry in log_entries), 1)
        labels = [entry["label"] for entry in web_app.build_dashboard()["activity"]]
        self.assertIn("Checked official flight DEMO-404 → cancelled", labels)
        self.assertIn("Read social posts → unverified, ignored", labels)

    def test_auto_replay_is_disclosed_on_dashboard(self):
        replay_file = self.data_dir / "replay" / "gemini_run.json"
        replay_file.parent.mkdir()
        replay_file.write_text(json.dumps([
            {"candidates": [{"content": {"role": "model", "parts": [{"text": "Demo complete."}]}}]},
        ]), encoding="utf-8")
        with mock.patch.object(agent, "REPLAY_PATH", str(replay_file)), \
             mock.patch.object(web_app, "load_env", return_value={"AI_MODE": "auto"}), \
             mock.patch.object(agent, "call_gemini", side_effect=agent.GeminiUnavailable(503, "busy")), \
             mock.patch("builtins.print"):
            self.assertEqual(self._post("/run-ai").status_code, 302)
        page = self.client.get("/").get_data(as_text=True)
        self.assertIn("Demo replay — recorded Gemini responses. Safety gate, approvals and PayPal run live.", page)
        self.assertIn("Demo replay used recorded Gemini responses", page)

    def test_timeout_in_web_ai_check_switches_to_replay(self):
        replay_file = self.data_dir / "replay" / "gemini_run.json"
        replay_file.parent.mkdir()
        replay_file.write_text(json.dumps([
            {"candidates": [{"content": {"role": "model", "parts": [{"text": "Demo complete."}]}}]},
        ]), encoding="utf-8")
        with mock.patch.object(agent, "REPLAY_PATH", str(replay_file)), \
             mock.patch.object(web_app, "load_env", return_value={}), \
             mock.patch.object(agent, "call_gemini", side_effect=TimeoutError("private failure detail")), \
             mock.patch("builtins.print"):
            self.assertEqual(self._post("/run-ai").status_code, 302)

        page = self.client.get("/").get_data(as_text=True)
        self.assertIn("Demo replay — recorded Gemini responses", page)
        self.assertIn("AI check completed. Review the proposals", page)
        self.assertNotIn("The AI check could not finish", page)
        self.assertNotIn("private failure detail", self.log_path.read_text(encoding="utf-8"))

    def test_quota_failure_uses_specific_english_message(self):
        with mock.patch.object(web_app, "load_env", return_value={"AI_MODE": "live"}), \
             mock.patch.object(agent, "call_gemini", side_effect=agent.GeminiUnavailable(429, "quota")), \
             mock.patch("builtins.print"):
            self.assertEqual(self._post("/run-ai").status_code, 302)
        self.assertIn("Gemini usage limit reached (429). No approval was performed.",
                      self.client.get("/").get_data(as_text=True))

    def test_approved_extension_consumes_room_inventory(self):
        guest_data = json.loads((self.data_dir / "guests.json").read_text(encoding="utf-8"))
        guest_data["vacant_rooms_for_extension"] = 1
        (self.data_dir / "guests.json").write_text(
            json.dumps(guest_data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        approved = self._pending_extension()
        approved["status"] = "approved"
        self._write_approvals([approved])

        result = agent.tool_propose_stay_extension(
            {
                "guest_id": "G002",
                "nights": 1,
                "reason": "Official cancellation confirmed.",
                "message_draft": "Safe message.",
            }
        )

        self.assertFalse(result["accepted"])
        self.assertTrue(any("空室がもうありません" in reason for reason in result["reasons"]))
        self.assertEqual(web_app.build_dashboard()["metrics"]["rooms_remaining"], 0)
        self.assertEqual(len(self._read_approvals()), 1)

    def test_one_approval_creates_one_paypal_invoice(self):
        self._write_approvals([self._pending_extension()])
        create_invoice = mock.Mock(return_value=("INV-TEST", "128.00"))
        send_invoice = mock.Mock(return_value=("SENT", "https://www.sandbox.paypal.com/invoice/test"))

        with mock.patch.object(approve, "load_env", return_value={"BUYER_EMAIL": "buyer@example.test"}), \
             mock.patch.object(approve, "create_extension_invoice", create_invoice), \
             mock.patch.object(approve, "send_invoice", send_invoice):
            response = self._post("/proposals/P001/approve")

        self.assertEqual(response.status_code, 302)
        create_invoice.assert_called_once()
        send_invoice.assert_called_once_with({"BUYER_EMAIL": "buyer@example.test"}, "INV-TEST")
        self.assertEqual(self._read_approvals()[0]["status"], "sent")

    def test_sandbox_payment_controls_only_appear_on_sent_cards(self):
        pending = self._pending_extension("P001")
        sent = self._pending_extension("P002")
        sent.update(status="sent", invoice_id="INV-SENT", pay_url="https://www.sandbox.paypal.com/invoice/test")
        confirmed = self._pending_extension("P003")
        confirmed.update(status="confirmed", invoice_id="INV-PAID")
        self._write_approvals([pending, sent, confirmed])

        page = self.client.get("/").get_data(as_text=True)
        self.assertEqual(page.count("Record sandbox payment"), 1)
        self.assertEqual(page.count("View guest invoice in PayPal"), 1)
        self.assertIn('target="_blank"', page)
        self.assertIn("Sandbox only — in production the guest pays in PayPal.", page)

        sent.pop("pay_url")
        self._write_approvals([pending, sent, confirmed])
        self.assertNotIn("View guest invoice in PayPal", self.client.get("/").get_data(as_text=True))

    def test_record_sandbox_payment_confirms_once_with_saved_amount(self):
        item = self._pending_extension()
        item.update(status="sent", invoice_id="INV-TEST", pay_url="https://www.sandbox.paypal.com/invoice/test")
        self._write_approvals([item])
        payment = mock.Mock()
        env = {"PAYPAL_CLIENT_ID": "test-client"}

        with mock.patch.object(web_app, "load_env", return_value=env), \
             mock.patch.object(web_app, "record_invoice_payment", payment), \
             mock.patch.object(check_payments, "_get_token", return_value="test-token"), \
             mock.patch.object(check_payments, "get_invoice_status", return_value="MARKED_AS_PAID"):
            token = self._csrf_token()
            first = self.client.post(
                "/proposals/P001/record-sandbox-payment",
                data={"csrf_token": token, "amount": "9999", "invoice_id": "INV-FORGED"},
            )
            second = self.client.post(
                "/proposals/P001/record-sandbox-payment", data={"csrf_token": token},
            )

        self.assertEqual((first.status_code, second.status_code), (302, 302))
        payment.assert_called_once_with(env, "INV-TEST", 128.0)
        saved = self._read_approvals()[0]
        self.assertEqual(saved["status"], "confirmed")
        self.assertTrue(saved["payment_recorded_by_manager"])
        page = self.client.get("/").get_data(as_text=True)
        self.assertIn("Paid (recorded by manager · sandbox)", page)
        self.assertNotIn("Record sandbox payment", page)
        self.assertIn("not eligible for sandbox payment recording", page)
        entries = [json.loads(line) for line in self.log_path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(
            [entry["detail"] for entry in entries if entry["action"] == "recorded_sandbox_payment"],
            [{"id": "P001", "invoice_id": "INV-TEST"}],
        )

    def test_record_sandbox_payment_failure_keeps_sent_and_releases_limit(self):
        item = self._pending_extension()
        item.update(status="sent", invoice_id="INV-TEST")
        self._write_approvals([item])
        with mock.patch.object(web_app, "load_env", return_value={}), \
             mock.patch.object(web_app, "record_invoice_payment", side_effect=RuntimeError("mock failure")) as payment:
            self.assertEqual(self._post("/proposals/P001/record-sandbox-payment").status_code, 302)
        payment.assert_called_once()
        self.assertEqual(self._read_approvals()[0]["status"], "sent")
        self.assertNotIn("payment_recorded_by_manager", self._read_approvals()[0])
        self.assertIn("PayPal could not record the sandbox payment", self.client.get("/").get_data(as_text=True))
        limits = json.loads((self.data_dir / "workspaces" / "_limits.json").read_text(encoding="utf-8"))
        self.assertEqual(limits["visitors"][self.workspace_id]["payment_records_total"], 0)

    def test_record_sandbox_payment_limit_and_csrf_block_paypal(self):
        item = self._pending_extension()
        item.update(status="sent", invoice_id="INV-TEST")
        self._write_approvals([item])
        with mock.patch.dict("os.environ", {"MAX_PAYMENT_RECORDS_PER_DAY_PER_VISITOR": "0"}), \
             mock.patch.object(web_app, "record_invoice_payment") as payment:
            self.assertEqual(self.client.get("/proposals/P001/record-sandbox-payment").status_code, 302)
            self.assertEqual(self.client.post("/proposals/P001/record-sandbox-payment").status_code, 400)
            self.assertEqual(self._post("/proposals/P001/record-sandbox-payment").status_code, 302)
        payment.assert_not_called()
        self.assertEqual(self._read_approvals()[0]["status"], "sent")
        self.assertIn("sandbox payment recording limit", self.client.get("/").get_data(as_text=True))

    def test_recorded_payment_check_failure_cannot_record_twice(self):
        item = self._pending_extension()
        item.update(status="sent", invoice_id="INV-TEST")
        self._write_approvals([item])
        with mock.patch.object(web_app, "load_env", return_value={}), \
             mock.patch.object(web_app, "record_invoice_payment") as payment, \
             mock.patch.object(web_app, "check_payments_action", side_effect=RuntimeError("mock failure")):
            self._post("/proposals/P001/record-sandbox-payment")
            self._post("/proposals/P001/record-sandbox-payment")
        payment.assert_called_once()
        saved = self._read_approvals()[0]
        self.assertEqual(saved["status"], "sent")
        self.assertTrue(saved["payment_recorded_by_manager"])
        page = self.client.get("/").get_data(as_text=True)
        self.assertIn("Waiting for PayPal status confirmation", page)
        self.assertNotIn("Record sandbox payment", page)

    def test_rejection_never_calls_paypal(self):
        self._write_approvals([self._pending_extension()])
        with mock.patch.object(approve, "create_extension_invoice") as create_invoice, \
             mock.patch.object(approve, "send_invoice") as send_invoice:
            response = self._post("/proposals/P001/reject")

        self.assertEqual(response.status_code, 302)
        create_invoice.assert_not_called()
        send_invoice.assert_not_called()
        self.assertEqual(self._read_approvals()[0]["status"], "rejected")

    def test_reset_archives_then_clears_local_demo_files(self):
        token = self._csrf_token()
        self._write_approvals([self._pending_extension()])
        self.log_path.write_text('{"time":"old","actor":"ai","action":"finished","detail":{}}\n', encoding="utf-8")

        with mock.patch.object(approve, "create_extension_invoice") as create_invoice, \
             mock.patch.object(approve, "send_invoice") as send_invoice:
            response = self.client.post("/reset-demo", data={"csrf_token": token})

        self.assertEqual(response.status_code, 302)
        create_invoice.assert_not_called()
        send_invoice.assert_not_called()
        self.assertEqual(self._read_approvals(), [])
        self.assertEqual(self.log_path.read_text(encoding="utf-8"), "")
        archives = list((self.workspace_dir / "archive").iterdir())
        self.assertEqual(len(archives), 1)
        self.assertTrue((archives[0] / "approvals.json").exists())
        archived_log = (archives[0] / "decision_log.jsonl").read_text(encoding="utf-8")
        self.assertIn('"action": "reset_demo"', archived_log)

    def test_brand_and_gate_use_required_display_values(self):
        common.log_decision(
            "gate",
            "rejected_proposal",
            {"guest_id": "G004", "args": {}, "reasons": ["OTA経由の予約です。"]},
        )
        response = self.client.get("/")
        page = response.get_data(as_text=True)
        with self.client.get("/static/style.css") as static_response:
            css = static_response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn("nankuru BANTŌ", page)
        self.assertIn("M+PLUS+Rounded+1c", page)
        self.assertIn("IBM+Plex+Sans+JP", page)
        self.assertIn("M PLUS Rounded 1c", css)
        self.assertIn("IBM Plex Sans JP", css)
        self.assertIn("favicon.svg", page)
        self.assertEqual(web_app.build_dashboard()["gate_events"][0]["guest_name"], "Liu Wei")
        self.assertNotIn("Inter", css)
        self.assertNotIn("Georgia", css)
        self.assertIn("#23305e", css)
        self.assertIn("#f4f2ec", css)
        self.assertIn("#15202b", css)

    def test_last_reset_anchors_demo_time_without_changing_audit_log(self):
        self._write_log_entries([
            {"time": "2026-10-15T09:00:00", "actor": "ai", "action": "finished", "detail": {}},
            {"time": "2026-10-15T12:00:00", "actor": "human", "action": "reset_demo", "detail": {}},
            {"time": "2026-10-15T12:03:00", "actor": "ai", "action": "finished", "detail": {}},
        ])

        page = self.client.get("/").get_data(as_text=True)

        self.assertIn("Data as of Aug 6 · 22:00", page)
        self.assertIn("Aug 6 · 22:03", page)
        self.assertEqual(
            json.loads(self.log_path.read_text(encoding="utf-8").splitlines()[-1])["time"],
            "2026-10-15T12:03:00",
        )

    def test_first_log_entry_anchors_demo_time_without_reset(self):
        self._write_log_entries([
            {"time": "2026-10-15T09:00:00", "actor": "ai", "action": "finished", "detail": {}},
            {"time": "2026-10-15T09:04:00", "actor": "ai", "action": "finished", "detail": {}},
        ])

        page = self.client.get("/").get_data(as_text=True)

        self.assertIn("Aug 6 · 22:00", page)
        self.assertIn("Aug 6 · 22:04", page)

    def test_demo_time_advances_to_next_date(self):
        self._write_log_entries([
            {"time": "2026-10-15T10:00:00", "actor": "human", "action": "reset_demo", "detail": {}},
            {"time": "2026-10-15T12:15:00", "actor": "gate", "action": "rejected_proposal",
             "detail": {"guest_id": "G004", "reasons": ["OTA経由の予約です。"]}},
        ])

        page = self.client.get("/").get_data(as_text=True)

        self.assertIn("Aug 7 · 00:15", page)
        self.assertEqual(page.count("Aug 7 · 00:15"), 2)

    def test_gate_activity_uses_guest_name_and_unknown_id_fallback(self):
        self._write_log_entries([
            {"time": "2026-10-15T10:00:00", "actor": "gate", "action": "rejected_proposal",
             "detail": {"guest_id": "G004", "reasons": ["OTA経由の予約です。"]}},
            {"time": "2026-10-15T10:01:00", "actor": "gate", "action": "rejected_proposal",
             "detail": {"guest_id": "G999", "reasons": ["OTA経由の予約です。"]}},
        ])

        page = self.client.get("/").get_data(as_text=True)

        self.assertIn("GATE · Liu Wei", page)
        self.assertIn("GATE · G999", page)
        self.assertNotIn("GATE · G004", page)

    def test_korean_hanja_draft_requests_rewrite_then_accepts_hangul(self):
        reason = "韓国語の案内文に漢字が含まれています。ハングルだけで書き直して、もう一度 propose_stay_extension を呼んでください。"
        args = {
            "guest_id": "G002",
            "nights": 1,
            "reason": "Official flight cancellation.",
            "message_draft": "DEMO-202편이 결航되었습니다.",
        }

        self.assertEqual(
            agent.tool_propose_stay_extension(args),
            {"accepted": False, "reasons": [reason]},
        )
        self.assertEqual(self._read_approvals(), [])
        entries = [json.loads(line) for line in self.log_path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["action"], "draft_rewrite_requested")
        self.assertEqual(entries[0]["detail"], {"guest_id": "G002", "reason": reason})
        self.assertNotIn(args["message_draft"], self.log_path.read_text(encoding="utf-8"))

        dashboard = web_app.build_dashboard()
        self.assertEqual(dashboard["gate_events"], [])
        self.assertEqual(dashboard["activity"][0]["label"], "Draft rewrite requested")

        args["message_draft"] = "DEMO-202편이 결항되었습니다. 연박 안내를 확인해 주세요."
        accepted = agent.tool_propose_stay_extension(args)
        self.assertTrue(accepted["accepted"])
        self.assertEqual(len(self._read_approvals()), 1)

    def test_korean_extension_a_character_also_requests_rewrite(self):
        result = agent.tool_propose_stay_extension({
            "guest_id": "G002", "nights": 1, "reason": "Official cancellation.",
            "message_draft": "㐀 결항되었습니다.",
        })
        self.assertFalse(result["accepted"])
        self.assertEqual(result["reasons"][0], "韓国語の案内文に漢字が含まれています。ハングルだけで書き直して、もう一度 propose_stay_extension を呼んでください。")
        self.assertEqual(self._read_approvals(), [])

    def test_chinese_and_japanese_drafts_with_han_characters_are_accepted(self):
        data_path = self.data_dir / "guests.json"
        data = json.loads(data_path.read_text(encoding="utf-8"))
        next(guest for guest in data["guests"] if guest["id"] == "G004")["booking_channel"] = "direct"
        data_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

        for guest_id, nights, draft in (
            ("G004", 1, "航班已取消，请确认延住安排。"),
            ("G003", 3, "便が欠航しました。延泊をご案内します。"),
        ):
            with self.subTest(guest_id=guest_id):
                result = agent.tool_propose_stay_extension({
                    "guest_id": guest_id, "nights": nights,
                    "reason": "Official cancellation.", "message_draft": draft,
                })
                self.assertTrue(result["accepted"])
        self.assertEqual(len(self._read_approvals()), 2)

    def test_ota_rejection_takes_priority_over_korean_draft_rewrite(self):
        data_path = self.data_dir / "guests.json"
        data = json.loads(data_path.read_text(encoding="utf-8"))
        next(guest for guest in data["guests"] if guest["id"] == "G004")["language"] = "Korean"
        data_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

        result = agent.tool_propose_stay_extension({
            "guest_id": "G004", "nights": 1, "reason": "Official cancellation.",
            "message_draft": "결航 안내입니다.",
        })
        self.assertFalse(result["accepted"])
        self.assertTrue(any("OTA" in reason for reason in result["reasons"]))
        self.assertEqual(self._read_approvals(), [])
        entries = [json.loads(line) for line in self.log_path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([entry["action"] for entry in entries], ["rejected_proposal"])


if __name__ == "__main__":
    unittest.main()
