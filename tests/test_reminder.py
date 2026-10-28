import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import agent
import app as web_app
import approve
import common
import paypal_tools


ROOT = Path(__file__).resolve().parents[1]


class ReminderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.data = Path(self.temp.name) / "data"
        self.workspace = self.data / "workspaces" / ("b" * 32)
        self.workspace.mkdir(parents=True)
        for name in ("guests.json", "flights.json", "flights_update.json"):
            shutil.copy(ROOT / "data" / name, self.data / name)
        self.queue_path = self.workspace / "approvals.json"
        self.queue_path.write_text("[]", encoding="utf-8")
        self.log_path = self.workspace / "decision_log.jsonl"
        self.log_path.write_text("", encoding="utf-8")
        self.patchers = [
            mock.patch.object(common, "DATA_DIR", str(self.data)),
            mock.patch.object(agent, "DATA_DIR", str(self.data)),
            mock.patch.object(web_app, "DATA_DIR", str(self.data)),
            mock.patch.object(agent, "load_env", return_value={}),
            mock.patch.object(approve, "load_env", return_value={}),
            mock.patch("urllib.request.urlopen", side_effect=AssertionError("Network forbidden")),
        ]
        for patcher in self.patchers:
            patcher.start()
        self.tokens = common.set_workspace_paths(str(self.workspace))
        web_app.app.config.update(TESTING=True, SECRET_KEY="test-reminder")
        self.client = web_app.app.test_client()
        with self.client.session_transaction() as state:
            state["workspace_id"] = "b" * 32

    def tearDown(self):
        common.reset_workspace_paths(self.tokens)
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.temp.cleanup()

    def save(self, items):
        common.write_json(str(self.queue_path), items)

    def queue(self):
        return common.read_json(str(self.queue_path), [])

    def actions(self):
        return [json.loads(line)["action"] for line in self.log_path.read_text(encoding="utf-8").splitlines()]

    def extension(self, guest_id="G001", status="sent"):
        return {"id": "P001", "type": "extension", "guest_id": guest_id,
                "guest_name": "Emily Carter" if guest_id == "G001" else "Kim Minji",
                "language": "English" if guest_id == "G001" else "Korean",
                "status": status, "invoice_id": "INV-1", "amount_usd": 128.0,
                "created_at": "2026-08-06T22:00:00"}

    def reminder(self):
        return {"id": "P002", "type": "payment_reminder", "proposal_id": "P001",
                "guest_id": "G001", "guest_name": "Emily Carter", "language": "English",
                "reason": "Still unpaid.", "message_draft": "Please pay to confirm your extension.",
                "status": "pending", "created_at": "2026-08-06T23:30:00"}

    def invoice_result(self, status="UNPAID"):
        return {"status": status, "due_amount": "128.00", "viewed_by_recipient": False,
                "last_sent_time": None}

    def csrf_post(self, path):
        self.client.get("/")
        with self.client.session_transaction() as state:
            token = state["csrf_token"]
        return self.client.post(path, data={"csrf_token": token})

    def test_read_invoice_returns_only_four_safe_fields(self):
        invoice = {"id": "INV-1", "status": "SENT", "due_amount": {"value": "128.00"},
                   "primary_recipients": [{"billing_info": {"email_address": "private@example.test"}}],
                   "detail": {"viewed_by_recipient": True,
                              "metadata": {"last_sent_time": "2026-08-06T22:00:00Z",
                                           "recipient_view_url": "https://private.example.test"}}}
        with mock.patch.object(paypal_tools, "_get_token", return_value="fake"), \
             mock.patch.object(paypal_tools, "_call", return_value=(200, invoice)) as call:
            result = paypal_tools.read_invoice({}, "INV-1")
        self.assertEqual(result, {"status": "SENT", "due_amount": "128.00",
                                  "viewed_by_recipient": True,
                                  "last_sent_time": "2026-08-06T22:00:00Z"})
        self.assertEqual(call.call_args.args, ("GET", "/v2/invoicing/invoices/INV-1"))
        with mock.patch.object(paypal_tools, "_get_token", return_value="fake"), \
             mock.patch.object(paypal_tools, "_call", return_value=(200, {"status": "PAID"})):
            self.assertEqual(paypal_tools.read_invoice({}, "INV-1"),
                             {"status": "PAID", "due_amount": None,
                              "viewed_by_recipient": False, "last_sent_time": None})

    def test_send_reminder_uses_sandbox_path_body_and_requires_204(self):
        self.assertEqual(paypal_tools.API_BASE, "https://api-m.sandbox.paypal.com")
        with mock.patch.object(paypal_tools, "_get_token", return_value="fake"), \
             mock.patch.object(paypal_tools, "_call", return_value=(204, {})) as call:
            paypal_tools.send_reminder({}, "INV-1", "Subject", "Note")
            self.assertEqual(call.call_args.args, ("POST", "/v2/invoicing/invoices/INV-1/remind"))
            self.assertEqual(call.call_args.kwargs["body"],
                             {"subject": "Subject", "note": "Note", "send_to_invoicer": True})
        with mock.patch.object(paypal_tools, "_get_token", return_value="fake"), \
             mock.patch.object(paypal_tools, "_call", return_value=(202, {})):
            with self.assertRaises(RuntimeError):
                paypal_tools.send_reminder({}, "INV-1", "Subject", "Note")

    def test_check_invoice_payment_hides_invoice_id_and_missing_sent_is_error(self):
        self.save([self.extension()])
        with mock.patch.object(agent, "read_invoice", return_value=self.invoice_result()) as read:
            result = agent.run_tool("check_invoice_payment", {"guest_id": "G001"})
        read.assert_called_once_with({}, "INV-1")
        self.assertEqual(result, {"guest_id": "G001", "paypal_status": "UNPAID",
                                  "due_amount": "128.00", "viewed_by_recipient": False})
        self.assertNotIn("INV-1", self.log_path.read_text(encoding="utf-8"))
        self.save([])
        self.assertIn("error", agent.tool_check_invoice_payment({"guest_id": "G001"}))

    def test_paypal_lookup_errors_return_safe_tool_results_and_gate_rejection(self):
        self.save([self.extension()])
        args = {"guest_id": "G001", "reason": "Still unpaid.", "message_draft": "Please pay."}
        for error in (RuntimeError("private"), SystemExit("private"), OSError("private")):
            with self.subTest(error=type(error).__name__), mock.patch.object(
                agent, "read_invoice", side_effect=error
            ) as read:
                self.assertEqual(agent.run_tool("check_invoice_payment", {"guest_id": "G001"}),
                                 {"error": "PayPal could not be reached. Try again later."})
                result = agent.run_tool("propose_payment_reminder", args)
                self.assertFalse(result["accepted"])
                self.assertIn("PayPal で請求書の状態を確認できませんでした。", result["reasons"])
                self.assertEqual(read.call_count, 2)
            self.assertEqual(self.actions()[-1], "rejected_reminder")
            self.assertEqual(len(self.queue()), 1)
        self.assertNotIn("private", self.log_path.read_text(encoding="utf-8"))
        self.assertIn("PayPal invoice status could not be checked. Try again later.",
                      self.client.get("/").get_data(as_text=True))

    def test_ai_run_continues_after_paypal_lookup_failure(self):
        self.save([self.extension()])
        calls = iter([
            {"candidates": [{"content": {"role": "model", "parts": [{"functionCall": {
                "name": "check_invoice_payment", "args": {"guest_id": "G001"}}}]}}]},
            {"candidates": [{"content": {"role": "model", "parts": [{"functionCall": {
                "name": "propose_payment_reminder", "args": {
                    "guest_id": "G001", "reason": "Still unpaid.", "message_draft": "Please pay."}}}]}}]},
            {"candidates": [{"content": {"role": "model", "parts": [{"text": "Finished safely."}]}}]},
        ])
        with mock.patch.object(agent, "read_invoice", side_effect=OSError("private")) as read:
            agent.run_agent(lambda _env, _contents: next(calls), {})
        self.assertEqual(read.call_count, 2)
        self.assertEqual(len(self.queue()), 1)
        self.assertIn("finished", self.actions())
        self.assertIn("rejected_reminder", self.actions())

    def test_reminder_gate_accepts_unpaid_and_rejects_duplicate(self):
        self.save([self.extension()])
        args = {"guest_id": "G001", "reason": "Still unpaid.",
                "message_draft": "Please pay to confirm your extension."}
        with mock.patch.object(agent, "read_invoice", return_value=self.invoice_result()):
            result = agent.tool_propose_payment_reminder(args)
            duplicate = agent.tool_propose_payment_reminder(args)
        self.assertTrue(result["accepted"])
        self.assertEqual(self.queue()[-1]["type"], "payment_reminder")
        self.assertNotIn("invoice_id", self.queue()[-1])
        self.assertFalse(duplicate["accepted"])
        self.assertEqual(len(self.queue()), 2)
        self.assertIn("proposed_reminder", self.actions())
        self.assertIn("rejected_reminder", self.actions())
        items = self.queue()
        items[-1]["status"] = "done"
        self.save(items)
        with mock.patch.object(agent, "read_invoice", return_value=self.invoice_result()):
            self.assertFalse(agent.tool_propose_payment_reminder(args)["accepted"])

    def test_reminder_gate_rejects_paid_restored_blank_and_korean_hanja(self):
        self.save([self.extension()])
        args = {"guest_id": "G001", "reason": "Still unpaid.", "message_draft": "Please pay."}
        with mock.patch.object(agent, "read_invoice", return_value=self.invoice_result("PAID")):
            self.assertFalse(agent.tool_propose_payment_reminder(args)["accepted"])
        common.write_json(common.scenario_path(), {"notice": "update"})
        self.save([self.extension("G002")])
        args["guest_id"] = "G002"
        with mock.patch.object(agent, "read_invoice", return_value=self.invoice_result()):
            restored = agent.tool_propose_payment_reminder(args)
            self.assertIn("propose_invoice_cancellation", " ".join(restored["reasons"]))
        common.write_json(common.scenario_path(), {"notice": "initial"})
        with mock.patch.object(agent, "read_invoice", return_value=self.invoice_result()):
            self.assertFalse(agent.tool_propose_payment_reminder({**args, "message_draft": " "})["accepted"])
            self.assertFalse(agent.tool_propose_payment_reminder({**args, "message_draft": "漢字가 있어요"})["accepted"])
        self.assertEqual(len(self.queue()), 1)

    def test_reminder_gate_requires_sent_extension_and_unpaid_paypal_status(self):
        args = {"guest_id": "G001", "reason": "Still unpaid.", "message_draft": "Please pay."}
        self.save([self.extension(status="approved")])
        with mock.patch.object(agent, "read_invoice") as read:
            self.assertFalse(agent.tool_propose_payment_reminder(args)["accepted"])
        read.assert_not_called()
        self.save([self.extension()])
        for status in ("PARTIALLY_PAID", "CANCELLED", "DRAFT"):
            with self.subTest(status=status), mock.patch.object(
                agent, "read_invoice", return_value=self.invoice_result(status)
            ):
                self.assertFalse(agent.tool_propose_payment_reminder(args)["accepted"])
        self.assertEqual(len(self.queue()), 1)

    def test_paid_before_approval_confirms_without_sending(self):
        self.save([self.extension(), self.reminder()])
        with mock.patch.object(approve, "read_invoice", return_value=self.invoice_result("PAID")), \
             mock.patch("check_payments.get_invoice_status", return_value="PAID"), \
             mock.patch.object(approve, "send_reminder") as send:
            result = approve.approve_reminder("P002", env={"test": "env"})
        self.assertEqual(result["status"], "superseded")
        self.assertEqual([item["status"] for item in self.queue()], ["confirmed", "superseded"])
        send.assert_not_called()
        self.assertIn("reminder_skipped_paid", self.actions())

    def test_send_failure_leaves_queue_unchanged_and_logs_failure(self):
        self.save([self.extension(), self.reminder()])
        before = self.queue_path.read_bytes()
        with mock.patch.object(approve, "read_invoice", return_value=self.invoice_result()), \
             mock.patch.object(approve, "send_reminder", side_effect=RuntimeError("private")):
            with self.assertRaises(RuntimeError):
                approve.approve_reminder("P002", env={"test": "env"})
        self.assertEqual(self.queue_path.read_bytes(), before)
        self.assertIn("reminder_failed", self.actions())
        self.assertNotIn("private", self.log_path.read_text(encoding="utf-8"))

    def test_web_approve_reject_and_queue_labels(self):
        self.save([self.extension(), self.reminder()])
        page = self.client.get("/").get_data(as_text=True)
        self.assertIn("SEND PAYMENT REMINDER · Emily Carter", page)
        self.assertIn("Checked in PayPal (read-only status lookup).", page)
        with mock.patch.object(approve, "read_invoice", return_value=self.invoice_result()), \
             mock.patch.object(approve, "send_reminder") as send:
            response = self.csrf_post("/reminders/P002/approve")
        self.assertEqual(response.status_code, 302)
        send.assert_called_once()
        self.assertEqual(self.queue()[-1]["status"], "done")
        self.assertIn("Reminder sent by PayPal", self.client.get("/").get_data(as_text=True))
        self.save([self.extension(), self.reminder()])
        self.csrf_post("/reminders/P002/reject")
        self.assertEqual(self.queue()[-1]["status"], "rejected")
        self.assertIn("manager_rejected_reminder", self.actions())

    def test_recording_requires_both_new_proposal_types(self):
        self.save([self.extension()])
        def add_cancellation(_model, _env):
            self.save(self.queue() + [{"type": "invoice_cancellation"}])
        with mock.patch.object(agent, "run_agent", side_effect=add_cancellation), \
             mock.patch.object(agent, "_save_recording") as save:
            with self.assertRaises(agent.GeminiUnavailable):
                agent.run_ai({}, record=True, recording_required_type="cancellation_and_reminder")
        save.assert_not_called()
        def add_both(_model, _env):
            self.save(self.queue() + [{"type": "invoice_cancellation"}, {"type": "payment_reminder"}])
        with mock.patch.object(agent, "run_agent", side_effect=add_both), \
             mock.patch.object(agent, "_save_recording") as save:
            self.assertEqual(agent.run_ai({}, record=True,
                             recording_required_type="cancellation_and_reminder"), "live")
        save.assert_called_once()


if __name__ == "__main__":
    unittest.main()
