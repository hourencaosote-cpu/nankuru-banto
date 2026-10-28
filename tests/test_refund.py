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


class RefundTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.data = Path(self.temp.name) / "data"
        self.workspace = self.data / "workspaces" / ("c" * 32)
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
            mock.patch.object(approve, "load_env", return_value={}),
            mock.patch("urllib.request.urlopen", side_effect=AssertionError("Network forbidden")),
        ]
        for patcher in self.patchers:
            patcher.start()
        self.tokens = common.set_workspace_paths(str(self.workspace))
        web_app.app.config.update(TESTING=True, SECRET_KEY="test-refund")
        self.client = web_app.app.test_client()
        with self.client.session_transaction() as state:
            state["workspace_id"] = "c" * 32

    def tearDown(self):
        common.reset_workspace_paths(self.tokens)
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.temp.cleanup()

    def queue(self):
        return common.read_json(str(self.queue_path), [])

    def save(self, items):
        common.write_json(str(self.queue_path), items)

    def actions(self):
        return [json.loads(line)["action"] for line in self.log_path.read_text(encoding="utf-8").splitlines()]

    def update(self):
        common.write_json(common.scenario_path(), {"notice": "update"})

    def extension(self, status="confirmed", guest_id="G003", amount=144.0):
        return {"id": "P001", "type": "extension", "guest_id": guest_id,
                "guest_name": "Ken Tanaka (田中 健)" if guest_id == "G003" else "Kim Minji",
                "language": "Japanese" if guest_id == "G003" else "Korean",
                "nights": 3, "status": status, "invoice_id": "INV-1", "amount_usd": amount,
                "created_at": "2026-08-06T22:00:00"}

    def refund(self, amount=144.0):
        return {"id": "P002", "type": "refund", "proposal_id": "P001", "guest_id": "G003",
                "guest_name": "Ken Tanaka (田中 健)", "language": "Japanese", "amount_usd": amount,
                "reason": "Flight restored", "message_draft": "便が再開しました。返金します。",
                "evidence": "DEMO-303: on_time / official", "refund_request_id": "nankuru-refund-fixed-id",
                "status": "pending", "created_at": "2026-08-06T23:30:00"}

    def payment(self, kind="PAYPAL"):
        return {"payment_id": "CAP-1", "type": kind,
                "amount": {"currency_code": "USD", "value": "144.00"}}

    def capture(self, status="COMPLETED"):
        return {"status": status, "amount": {"currency_code": "USD", "value": "144.00"}}

    def post(self, path):
        self.client.get("/")
        with self.client.session_transaction() as state:
            token = state["csrf_token"]
        return self.client.post(path, data={"csrf_token": token})

    def test_gate_accepts_confirmed_full_code_amount_and_blocks_duplicates(self):
        self.update()
        self.save([self.extension()])
        declaration = next(tool for tool in agent.TOOLS if tool["name"] == "propose_refund")
        self.assertEqual(declaration["parameters"]["required"], ["guest_id", "reason", "message_draft"])
        self.assertNotIn("amount_usd", declaration["parameters"]["properties"])
        self.assertNotIn("refund_capture", agent.TOOL_FUNCTIONS)
        args = {"guest_id": "G003", "reason": "Flight restored", "message_draft": "便が再開しました。",
                "amount_usd": 9999}
        result = agent.tool_propose_refund(args)
        self.assertTrue(result["accepted"])
        saved = self.queue()[-1]
        self.assertEqual(saved["amount_usd"], 144.0)
        self.assertEqual(saved["status"], "pending")
        self.assertTrue(saved["refund_request_id"].startswith("nankuru-refund-"))
        self.assertIn("DEMO-303: on_time", saved["evidence"])
        self.assertEqual(self.actions()[-1], "proposed_refund")
        self.assertFalse(agent.tool_propose_refund(args)["accepted"])
        self.assertEqual(self.actions()[-1], "rejected_refund")

    def test_gate_blocks_unpaid_cancelled_blank_and_korean_hanja(self):
        self.update()
        args = {"guest_id": "G003", "reason": "Flight restored", "message_draft": "便が再開しました。"}
        self.save([self.extension("sent")])
        self.assertIn("propose_invoice_cancellation", agent.tool_propose_refund(args)["reasons"][0])
        self.save([self.extension()])
        common.write_json(common.scenario_path(), {"notice": "initial"})
        self.assertFalse(agent.tool_propose_refund(args)["accepted"])
        self.update()
        self.assertFalse(agent.tool_propose_refund({**args, "message_draft": " "})["accepted"])
        self.save([self.extension(guest_id="G002")])
        korean = {"guest_id": "G002", "reason": "Restored", "message_draft": "漢字가 있습니다."}
        self.assertFalse(agent.tool_propose_refund(korean)["accepted"])
        self.assertTrue(all(action == "rejected_refund" for action in self.actions()))

    def test_paypal_capture_refund_uses_saved_id_and_updates_statuses(self):
        self.update()
        self.save([self.extension(), self.refund()])
        with mock.patch.object(approve, "get_invoice_payment", return_value=self.payment()) as get_payment, \
             mock.patch.object(approve, "get_capture", return_value=self.capture()) as get_capture, \
             mock.patch.object(approve, "refund_capture", return_value={"refund_id": "RF-1", "status": "COMPLETED"}) as issue:
            result = approve.approve_refund("P002", env={})
        self.assertEqual(result["status"], "done")
        get_payment.assert_called_once_with({}, "INV-1")
        get_capture.assert_called_once_with({}, "CAP-1")
        issue.assert_called_once_with({}, "CAP-1", 144.0, "nankuru-refund-fixed-id", "便が再開しました。返金します。")
        self.assertEqual([item["status"] for item in self.queue()], ["refunded", "done"])
        self.assertEqual(self.queue()[1]["refund_id"], "RF-1")
        self.assertEqual(self.queue()[1]["method"], "paypal")
        self.assertEqual(self.queue()[1]["refund_status"], "COMPLETED")
        self.assertEqual(self.actions()[-1], "approved_refund")

    def test_pending_refund_status_is_saved_and_shown(self):
        self.update()
        self.save([self.extension(), self.refund()])
        with mock.patch.object(approve, "get_invoice_payment", return_value=self.payment()), \
             mock.patch.object(approve, "get_capture", return_value=self.capture()), \
             mock.patch.object(approve, "refund_capture", return_value={"refund_id": "RF-P", "status": "PENDING"}):
            self.assertEqual(self.post("/refunds/P002/approve").status_code, 302)
        saved = self.queue()
        self.assertEqual([item["status"] for item in saved], ["refunded", "done"])
        self.assertEqual(saved[1]["refund_status"], "PENDING")
        self.assertEqual(saved[1]["refund_id"], "RF-P")
        self.assertIn("Refund pending in PayPal", self.client.get("/").get_data(as_text=True))

    def test_external_payment_records_refund(self):
        self.update()
        self.save([self.extension(), self.refund()])
        with mock.patch.object(approve, "get_invoice_payment", return_value=self.payment("EXTERNAL")), \
             mock.patch.object(approve, "get_capture") as get_capture, \
             mock.patch.object(approve, "record_external_refund", return_value={"refund_id": "RF-2"}) as record:
            approve.approve_refund("P002", env={})
        get_capture.assert_not_called()
        record.assert_called_once_with({}, "INV-1", 144.0)
        self.assertEqual(self.queue()[1]["method"], "external")

    def test_non_completed_capture_is_superseded_without_refund(self):
        self.update()
        self.save([self.extension(), self.refund()])
        with mock.patch.object(approve, "get_invoice_payment", return_value=self.payment()), \
             mock.patch.object(approve, "get_capture", return_value=self.capture("REFUNDED")), \
             mock.patch.object(approve, "refund_capture") as issue:
            result = approve.approve_refund("P002", env={})
        issue.assert_not_called()
        self.assertEqual(result["status"], "superseded")
        self.assertEqual([item["status"] for item in self.queue()], ["confirmed", "superseded"])
        self.assertEqual(self.actions()[-1], "refund_skipped_not_refundable")

    def test_approval_rechecks_official_notice(self):
        self.save([self.extension(), self.refund()])
        before = self.queue_path.read_bytes()
        with mock.patch.object(approve, "get_invoice_payment") as get_payment:
            with self.assertRaisesRegex(ValueError, "no longer shows"):
                approve.approve_refund("P002", env={})
        get_payment.assert_not_called()
        self.assertEqual(self.queue_path.read_bytes(), before)
        self.assertEqual(self.actions()[-1], "refund_blocked_notice_changed")

    def test_failure_preserves_pending_and_reuses_id(self):
        self.update()
        self.save([self.extension(), self.refund()])
        before = self.queue_path.read_bytes()
        with mock.patch.object(approve, "get_invoice_payment", return_value=self.payment()), \
             mock.patch.object(approve, "get_capture", return_value=self.capture()), \
             mock.patch.object(approve, "refund_capture", side_effect=[RuntimeError("private"), {"refund_id": "RF-1", "status": "COMPLETED"}]) as issue:
            with self.assertRaises(RuntimeError):
                approve.approve_refund("P002", env={})
            self.assertEqual(self.queue_path.read_bytes(), before)
            self.assertEqual(self.actions()[-1], "refund_failed")
            approve.approve_refund("P002", env={})
        self.assertEqual(issue.call_count, 2)
        self.assertEqual(issue.call_args_list[0].args[3], issue.call_args_list[1].args[3])
        self.assertEqual([item["status"] for item in self.queue()], ["refunded", "done"])
        self.assertNotIn("private", self.log_path.read_text(encoding="utf-8"))

    def test_rejection_and_refunded_metrics_and_web_routes(self):
        self.update()
        self.save([self.extension(), self.refund()])
        page = self.client.get("/").get_data(as_text=True)
        self.assertIn("REFUND · Ken Tanaka", page)
        self.assertIn("USD 144.00 · full refund, set by code", page)
        self.assertIn("Nothing is refunded until you approve.", page)
        self.assertIn("Approve &amp; refund via PayPal", page)
        self.assertEqual(self.post("/refunds/P002/reject").status_code, 302)
        self.assertEqual([item["status"] for item in self.queue()], ["confirmed", "rejected"])
        self.assertEqual(self.actions()[-1], "manager_rejected_refund")
        self.save([self.extension("refunded"), {**self.refund(), "status": "done", "method": "external"}])
        dashboard = web_app.build_dashboard()
        self.assertEqual(dashboard["metrics"]["rooms_remaining"], dashboard["metrics"]["room_capacity"])
        self.assertEqual(dashboard["metrics"]["paid_revenue"], 0)
        self.assertEqual(dashboard["metrics"]["refunded_total"], 144.0)
        self.assertIn("Refund recorded in PayPal (sandbox external payment)", self.client.get("/").get_data(as_text=True))
        self.save([self.extension(), self.refund()])
        with mock.patch.object(approve, "get_invoice_payment", return_value=self.payment()), \
             mock.patch.object(approve, "get_capture", return_value=self.capture()), \
             mock.patch.object(approve, "refund_capture", return_value={"refund_id": "RF-1", "status": "COMPLETED"}):
            self.assertEqual(self.post("/refunds/P002/approve").status_code, 302)
        self.assertEqual([item["status"] for item in self.queue()], ["refunded", "done"])
        self.assertIn("Refunded in PayPal", self.client.get("/").get_data(as_text=True))

    def test_paypal_api_shapes_and_success_codes(self):
        invoice = {"payments": {"transactions": [{"payment_id": "CAP-1", "type": "PAYPAL",
                     "amount": {"currency_code": "USD", "value": "144.00"}, "payer_email": "private@example.test"}]}}
        with mock.patch.object(paypal_tools, "_get_token", return_value="token"), \
             mock.patch.object(paypal_tools, "_call", return_value=(200, invoice)):
            self.assertEqual(paypal_tools.get_invoice_payment({}, "INV-1"), self.payment())
        with mock.patch.object(paypal_tools, "_get_token", return_value="token"), \
             mock.patch.object(paypal_tools, "_call", return_value=(200, {"payments": {"transactions": []}})):
            self.assertIsNone(paypal_tools.get_invoice_payment({}, "INV-1"))
        with mock.patch.object(paypal_tools, "_get_token", return_value="token"), \
             mock.patch.object(paypal_tools, "_call", return_value=(200, {"status": "COMPLETED",
                 "amount": {"currency_code": "USD", "value": "144.00"}, "payer": {"secret": "no"}})):
            self.assertEqual(paypal_tools.get_capture({}, "CAP-1"), self.capture())
        for code in (200, 201):
            with self.subTest(code=code), mock.patch.object(paypal_tools, "_get_token", return_value="token"), \
                 mock.patch.object(paypal_tools, "_call", return_value=(code, {"id": "RF-1", "status": "COMPLETED"})) as call:
                self.assertEqual(paypal_tools.refund_capture({}, "CAP-1", 144, "stable-id", "x" * 300)["refund_id"], "RF-1")
                self.assertEqual(call.call_args.args, ("POST", "/v2/payments/captures/CAP-1/refund"))
                self.assertEqual(call.call_args.kwargs["body"], {"amount": {"currency_code": "USD", "value": "144.00"},
                                                       "note_to_payer": "x" * 255})
                self.assertEqual(call.call_args.kwargs["headers"], {"PayPal-Request-Id": "stable-id"})
        with mock.patch.object(paypal_tools, "_get_token", return_value="token"), \
             mock.patch.object(paypal_tools, "_call", return_value=(201, {"id": "RF-P", "status": "PENDING"})):
            self.assertEqual(paypal_tools.refund_capture({}, "CAP-1", 144, "stable-id", "note"),
                             {"refund_id": "RF-P", "status": "PENDING"})
        with mock.patch.object(paypal_tools, "_get_token", return_value="token"), \
             mock.patch.object(paypal_tools, "_call", return_value=(200, {"refund_id": "RF-2"})) as call:
            self.assertEqual(paypal_tools.record_external_refund({}, "INV-1", 144), {"refund_id": "RF-2"})
            self.assertEqual(call.call_args.args, ("POST", "/v2/invoicing/invoices/INV-1/refunds"))
            self.assertEqual(call.call_args.kwargs["body"], {"method": "OTHER", "amount": {"currency_code": "USD", "value": "144.00"}})
        with mock.patch.object(paypal_tools, "_get_token", return_value="token"), \
             mock.patch.object(paypal_tools, "_call", return_value=(422, {})):
            with self.assertRaises(RuntimeError):
                paypal_tools.refund_capture({}, "CAP-1", 144, "stable-id", "note")
            with self.assertRaises(RuntimeError):
                paypal_tools.record_external_refund({}, "INV-1", 144)
        for body_status in ("FAILED", "CANCELLED", None):
            with self.subTest(status=body_status), \
                 mock.patch.object(paypal_tools, "_get_token", return_value="token"), \
                 mock.patch.object(paypal_tools, "_call", return_value=(201, {"status": body_status})):
                with self.assertRaises(RuntimeError):
                    paypal_tools.refund_capture({}, "CAP-1", 144, "stable-id", "note")

    def test_failed_paypal_response_keeps_pending_for_same_id_retry(self):
        self.update()
        self.save([self.extension(), self.refund()])
        before = self.queue_path.read_bytes()
        with mock.patch.object(approve, "get_invoice_payment", return_value=self.payment()), \
             mock.patch.object(approve, "get_capture", return_value=self.capture()), \
             mock.patch.object(paypal_tools, "_get_token", return_value="token"), \
             mock.patch.object(paypal_tools, "_call", side_effect=[
                 (201, {"id": "RF-X", "status": "FAILED"}),
                 (200, {"id": "RF-Y", "status": "COMPLETED"}),
             ]) as call:
            with self.assertRaises(RuntimeError):
                approve.approve_refund("P002", env={})
            self.assertEqual(self.queue_path.read_bytes(), before)
            self.assertEqual(self.actions()[-1], "refund_failed")
            approve.approve_refund("P002", env={})
        self.assertEqual(call.call_count, 2)
        self.assertEqual(call.call_args_list[0].kwargs["headers"],
                         call.call_args_list[1].kwargs["headers"])
        self.assertEqual(call.call_args_list[0].kwargs["headers"],
                         {"PayPal-Request-Id": "nankuru-refund-fixed-id"})
        self.assertEqual([item["status"] for item in self.queue()], ["refunded", "done"])

    def test_call_merges_request_id_header(self):
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.status = 201
        response.read.return_value = b'{"id":"RF-1"}'
        with mock.patch.object(paypal_tools.urllib.request, "urlopen", return_value=response) as open_request:
            status, result = paypal_tools._call("POST", "/v2/payments/captures/CAP-1/refund",
                                                token="fake", body={"amount": {}},
                                                headers={"PayPal-Request-Id": "stable-id"})
        self.assertEqual((status, result), (201, {"id": "RF-1"}))
        request = open_request.call_args.args[0]
        self.assertEqual(request.get_header("Paypal-request-id"), "stable-id")
        self.assertEqual(request.get_header("Authorization"), "Bearer fake")

    def test_recording_requires_all_three_new_proposals(self):
        def append_kinds(kinds):
            self.save([{"id": f"P{index:03d}", "type": kind} for index, kind in enumerate(kinds, 1)])

        with mock.patch.object(agent, "run_agent", side_effect=lambda *_: append_kinds(
                ["invoice_cancellation", "payment_reminder"])), \
             mock.patch.object(agent, "_save_recording") as save_recording:
            with self.assertRaises(agent.GeminiUnavailable):
                agent.run_ai({}, call_model=lambda *_: {}, mode="live", record=True,
                             recording_required_type="cancellation_reminder_refund")
        save_recording.assert_not_called()
        self.save([])
        with mock.patch.object(agent, "run_agent", side_effect=lambda *_: append_kinds(
                ["invoice_cancellation", "payment_reminder", "refund"])), \
             mock.patch.object(agent, "_save_recording") as save_recording:
            self.assertEqual(agent.run_ai({}, call_model=lambda *_: {}, mode="live", record=True,
                                          recording_required_type="cancellation_reminder_refund"), "live")
        save_recording.assert_called_once()


if __name__ == "__main__":
    unittest.main()
