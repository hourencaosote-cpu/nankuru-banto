import json
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
        self.approvals_path = self.data_dir / "approvals.json"
        self.log_path = self.log_dir / "decision_log.jsonl"
        self._write_approvals([])
        self.log_path.write_text("", encoding="utf-8")

        replacements = (
            (agent, "DATA_DIR", str(self.data_dir)),
            (agent, "APPROVALS_PATH", str(self.approvals_path)),
            (approve, "APPROVALS_PATH", str(self.approvals_path)),
            (check_payments, "APPROVALS_PATH", str(self.approvals_path)),
            (common, "DATA_DIR", str(self.data_dir)),
            (common, "APPROVALS_PATH", str(self.approvals_path)),
            (common, "LOG_DIR", str(self.log_dir)),
            (common, "LOG_PATH", str(self.log_path)),
            (web_app, "DATA_DIR", str(self.data_dir)),
            (web_app, "APPROVALS_PATH", str(self.approvals_path)),
            (web_app, "LOG_PATH", str(self.log_path)),
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

        web_app.app.config.update(TESTING=True, SECRET_KEY="test-only-secret")
        self.client = web_app.app.test_client()

    def tearDown(self):
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

    def test_post_requires_csrf_and_approval_get_is_not_allowed(self):
        self.assertEqual(self.client.post("/run-ai").status_code, 400)
        self.assertEqual(self.client.get("/proposals/P001/approve").status_code, 405)

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
        archives = list((self.data_dir / "archive").iterdir())
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


if __name__ == "__main__":
    unittest.main()
