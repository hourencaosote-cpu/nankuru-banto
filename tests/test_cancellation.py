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


class CancellationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.data = self.root / "data"
        self.workspace = self.data / "workspaces" / ("a" * 32)
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
            mock.patch("urllib.request.urlopen", side_effect=AssertionError("Network forbidden")),
        ]
        for patch in self.patchers:
            patch.start()
        self.tokens = common.set_workspace_paths(str(self.workspace))
        web_app.app.config.update(TESTING=True, SECRET_KEY="test-cancel")
        self.client = web_app.app.test_client()
        with self.client.session_transaction() as state:
            state["workspace_id"] = "a" * 32

    def tearDown(self):
        common.reset_workspace_paths(self.tokens)
        for patch in reversed(self.patchers):
            patch.stop()
        self.temp.cleanup()

    def queue(self):
        return common.read_json(str(self.queue_path), [])

    def save(self, items):
        common.write_json(str(self.queue_path), items)

    def entries(self):
        return [json.loads(line) for line in self.log_path.read_text(encoding="utf-8").splitlines()]

    def extension(self, status="sent"):
        return {"id": "P001", "type": "extension", "guest_id": "G002", "guest_name": "Kim Minji",
                "language": "Korean", "nights": 1, "status": status, "invoice_id": "INV-1",
                "amount_usd": 64, "nightly_rate_usd": 80, "discount_percent": 20,
                "message_draft": "연박 안내", "reason": "Cancelled flight", "evidence": "DEMO-202",
                "created_at": "2026-08-06T22:00:00"}

    def cancellation(self):
        return {"id": "P002", "type": "invoice_cancellation", "proposal_id": "P001",
                "guest_id": "G002", "guest_name": "Kim Minji", "language": "Korean",
                "reason": "Flight restored.", "message_draft": "항공편이 재개되어 연박이 필요하지 않습니다. 청구서를 취소하므로 결제하지 않으셔도 됩니다.",
                "evidence": "DEMO-202: on_time", "status": "pending", "created_at": "2026-08-06T23:30:00"}

    def update(self):
        common.write_json(common.scenario_path(), {"notice": "update"})

    def csrf_post(self, path):
        self.client.get("/")
        with self.client.session_transaction() as state:
            token = state["csrf_token"]
        return self.client.post(path, data={"csrf_token": token})

    def test_update_data_only_changes_kim_ken_and_as_of(self):
        initial = common.read_json(str(self.data / "flights.json"), {})
        updated = common.read_json(str(self.data / "flights_update.json"), {})
        self.assertEqual(updated["as_of"], "2026-08-06 23:30")
        self.assertEqual(updated["flights"]["DEMO-202"]["status"], "on_time")
        self.assertIsNone(updated["flights"]["DEMO-202"]["next_available_date"])
        self.assertEqual(updated["flights"]["DEMO-303"]["status"], "on_time")
        self.assertIsNone(updated["flights"]["DEMO-303"]["next_available_date"])
        self.assertEqual(updated["flights"]["DEMO-303"]["official_source"],
                         "Demo Airlines official update (fictional): service restored")
        changed = {"DEMO-202", "DEMO-303"}
        self.assertEqual({k: v for k, v in updated["flights"].items() if k not in changed},
                         {k: v for k, v in initial["flights"].items() if k not in changed})
        self.assertEqual(updated["unverified_social_posts"], initial["unverified_social_posts"])

    def test_gate_accepts_restored_sent_invoice_and_private_list(self):
        self.save([self.extension()])
        before = self.queue_path.read_bytes()
        listed = agent.tool_list_open_invoices({})
        self.assertEqual(self.queue_path.read_bytes(), before)
        self.assertEqual(listed["extensions"], [{"proposal_id": "P001", "guest_id": "G002",
                                                 "guest_name": "Kim Minji", "nights": 1, "status": "sent"}])
        self.assertNotIn("INV-1", json.dumps(listed))
        self.update()
        result = agent.tool_propose_invoice_cancellation({"guest_id": "G002", "reason": "Flight restored.",
                                                           "message_draft": "연박이 필요하지 않습니다."})
        self.assertTrue(result["accepted"])
        item = self.queue()[-1]
        self.assertEqual(item["type"], "invoice_cancellation")
        self.assertIn("(as of 2026-08-06 23:30)", item["evidence"])
        self.assertNotIn("invoice_id", item)
        self.assertEqual(self.entries()[-1]["action"], "proposed_cancellation")

    def test_cancellation_tool_resolves_extension_from_guest_id(self):
        extension = {**self.extension("approved"), "id": "P009"}
        self.save([extension])
        self.update()
        declaration = next(tool for tool in agent.TOOLS if tool["name"] == "propose_invoice_cancellation")
        self.assertEqual(declaration["parameters"]["required"], ["guest_id", "reason", "message_draft"])
        self.assertNotIn("proposal_id", declaration["parameters"]["properties"])
        result = agent.tool_propose_invoice_cancellation({
            "guest_id": "G002", "reason": "Flight restored.", "message_draft": "연박이 필요하지 않습니다.",
        })
        self.assertTrue(result["accepted"])
        self.assertEqual(self.queue()[-1]["proposal_id"], "P009")
        missing = agent.tool_propose_invoice_cancellation({
            "guest_id": "G999", "reason": "No extension.", "message_draft": "No payment is needed.",
        })
        self.assertFalse(missing["accepted"])
        self.assertIn("存在しません", missing["reasons"][0])

    def test_approval_rechecks_official_notice_before_create_or_saved_draft_send(self):
        self.update()
        message = "The official notice no longer shows this guest’s flight as cancelled. No invoice was created."
        for status in ("pending", "approved"):
            with self.subTest(status=status):
                item = self.extension(status)
                if status == "pending":
                    item.pop("invoice_id")
                self.save([item])
                before = self.queue_path.read_bytes()
                before_create = mock.Mock()
                on_invoice_ready = mock.Mock()
                with mock.patch.object(approve, "create_extension_invoice") as create, \
                     mock.patch.object(approve, "send_invoice") as send, \
                     mock.patch.object(approve, "load_env") as load_env, \
                     mock.patch.object(approve, "get_invoice_status") as get_status:
                    with self.assertRaisesRegex(ValueError, "The official notice no longer shows") as caught:
                        approve.approve_extension("P001", before_create=before_create,
                                                  on_invoice_ready=on_invoice_ready)
                self.assertEqual(str(caught.exception), message)
                self.assertEqual(self.queue_path.read_bytes(), before)
                before_create.assert_not_called()
                on_invoice_ready.assert_not_called()
                create.assert_not_called()
                send.assert_not_called()
                load_env.assert_not_called()
                get_status.assert_not_called()
                entry = self.entries()[-1]
                self.assertEqual(entry["action"], "approval_blocked_notice_changed")
                self.assertEqual(entry["detail"], {"id": "P001", "guest_id": "G002",
                                                   "flight": "DEMO-202", "status": "on_time"})
                self.assertEqual(web_app.ACTION_LABELS[entry["action"]],
                                 "Approval blocked: flight no longer cancelled")
                self.assertIn("Approval blocked: flight no longer cancelled",
                              self.client.get("/").get_data(as_text=True))
        self.assertEqual(self.csrf_post("/proposals/P001/approve").status_code, 302)
        self.assertIn(message, self.client.get("/").get_data(as_text=True))

    def test_gate_rejects_initial_paid_other_status_duplicate_blank_and_hanja(self):
        args = {"guest_id": "G002", "reason": "Flight restored.", "message_draft": "연박이 필요하지 않습니다."}
        self.save([self.extension()])
        self.assertFalse(agent.tool_propose_invoice_cancellation(args)["accepted"])
        self.update()
        for status in ("confirmed", "pending", "rejected", "cancelled"):
            self.save([self.extension(status)])
            result = agent.tool_propose_invoice_cancellation(args)
            self.assertFalse(result["accepted"])
            if status == "confirmed":
                self.assertIn("propose_refund", result["reasons"][0])
        self.save([self.extension(), self.cancellation()])
        self.assertFalse(agent.tool_propose_invoice_cancellation(args)["accepted"])
        self.save([self.extension()])
        self.assertFalse(agent.tool_propose_invoice_cancellation({**args, "message_draft": " "})["accepted"])
        rewrite = agent.tool_propose_invoice_cancellation({**args, "message_draft": "漢字가 있어요"})
        self.assertFalse(rewrite["accepted"])
        self.assertIn("書き直して", rewrite["reasons"][0])
        self.assertTrue(all(entry["action"] == "rejected_cancellation" for entry in self.entries()))

    def test_manager_cancels_sent_or_deletes_draft(self):
        for status in ("sent", "approved"):
            with self.subTest(status=status):
                self.save([self.extension(status), self.cancellation()])
                with mock.patch.object(approve, "get_invoice_status", return_value="SENT" if status == "sent" else "DRAFT"), \
                     mock.patch.object(approve, "cancel_invoice") as cancel, \
                     mock.patch.object(approve, "delete_invoice_draft") as delete:
                    result = approve.approve_cancellation("P002", env={"test": "value"})
                self.assertEqual(result["status"], "done")
                self.assertEqual([item["status"] for item in self.queue()], ["cancelled", "done"])
                self.assertEqual(cancel.call_count, int(status == "sent"))
                self.assertEqual(delete.call_count, int(status == "approved"))

    def test_paid_is_confirmed_and_never_cancelled(self):
        self.save([self.extension(), self.cancellation()])
        with mock.patch.object(approve, "get_invoice_status", return_value="PAID"), \
             mock.patch("check_payments.get_invoice_status", return_value="PAID"), \
             mock.patch.object(approve, "cancel_invoice") as cancel:
            result = approve.approve_cancellation("P002", env={"test": "value"})
        self.assertEqual(result["status"], "superseded")
        self.assertEqual([item["status"] for item in self.queue()], ["confirmed", "superseded"])
        cancel.assert_not_called()
        self.assertIn("cancellation_skipped_paid", [entry["action"] for entry in self.entries()])
        payment = next(entry for entry in self.entries() if entry["action"] == "payment_confirmed")
        self.assertEqual(payment["detail"]["guest_id"], "G002")

    def test_paypal_failure_keeps_statuses_and_rejection_keeps_extension(self):
        self.save([self.extension(), self.cancellation()])
        with mock.patch.object(approve, "get_invoice_status", return_value="SENT"), \
             mock.patch.object(approve, "cancel_invoice", side_effect=RuntimeError("private")):
            with self.assertRaises(RuntimeError):
                approve.approve_cancellation("P002", env={"test": "value"})
        self.assertEqual([item["status"] for item in self.queue()], ["sent", "pending"])
        self.assertEqual(self.entries()[-1]["action"], "cancellation_failed")
        self.assertNotIn("private", self.log_path.read_text(encoding="utf-8"))
        approve.reject_cancellation("P002")
        self.assertEqual([item["status"] for item in self.queue()], ["sent", "rejected"])
        self.assertEqual(self.entries()[-1]["action"], "manager_rejected_cancellation")

    def test_partial_payment_is_not_eligible_for_cancellation(self):
        self.save([self.extension(), self.cancellation()])
        with mock.patch.object(approve, "get_invoice_status", return_value="PARTIALLY_PAID"), \
             mock.patch.object(approve, "cancel_invoice") as cancel:
            with self.assertRaises(RuntimeError):
                approve.approve_cancellation("P002", env={"test": "value"})
        cancel.assert_not_called()
        self.assertEqual([item["status"] for item in self.queue()], ["sent", "pending"])

    def test_web_cancellation_routes_require_post_and_use_manager_action(self):
        self.save([self.extension(), self.cancellation()])
        self.assertEqual(self.client.get("/cancellations/P002/approve").status_code, 302)
        self.assertEqual([item["status"] for item in self.queue()], ["sent", "pending"])
        with mock.patch.object(web_app, "approve_cancellation", return_value={"status": "done"}) as approve_action:
            self.assertEqual(self.csrf_post("/cancellations/P002/approve").status_code, 302)
        approve_action.assert_called_once_with("P002")
        self.assertEqual(self.csrf_post("/cancellations/P002/reject").status_code, 302)
        self.assertEqual([item["status"] for item in self.queue()], ["sent", "rejected"])

    def test_paypal_api_shapes_and_failures(self):
        with mock.patch.object(paypal_tools, "_get_token", return_value="token"), \
             mock.patch.object(paypal_tools, "_call", return_value=(204, {})) as call:
            paypal_tools.cancel_invoice({}, "INV-1", "Guest note")
            self.assertEqual(call.call_args.args, ("POST", "/v2/invoicing/invoices/INV-1/cancel"))
            self.assertEqual(call.call_args.kwargs["body"], {
                "subject": "Your stay extension invoice was cancelled", "note": "Guest note",
                "send_to_recipient": True, "send_to_invoicer": True})
            paypal_tools.delete_invoice_draft({}, "INV-1")
            self.assertEqual(call.call_args.args, ("DELETE", "/v2/invoicing/invoices/INV-1"))
        with mock.patch.object(paypal_tools, "_get_token", return_value="token"), \
             mock.patch.object(paypal_tools, "_call", return_value=(422, {})):
            with self.assertRaises(RuntimeError):
                paypal_tools.cancel_invoice({}, "INV-1", "Guest note")
            with self.assertRaises(RuntimeError):
                paypal_tools.delete_invoice_draft({}, "INV-1")

    def test_update_button_reset_and_cancelled_dashboard(self):
        self.assertEqual(self.client.get("/demo/airline-update").status_code, 302)
        self.assertEqual(common.notice_scenario(), "initial")
        self.assertIn("Simulate 23:30 airline update", self.client.get("/").get_data(as_text=True))
        self.assertEqual(self.csrf_post("/demo/airline-update").status_code, 302)
        self.assertEqual(common.notice_scenario(), "update")
        self.assertEqual(self.entries()[-1]["detail"], {"as_of": "2026-08-06 23:30"})
        self.save([self.extension("cancelled"), {**self.cancellation(), "status": "done"}])
        page = self.client.get("/").get_data(as_text=True)
        self.assertIn("Official notice as of Aug 6 · 23:30", page)
        self.assertNotIn("Simulate 23:30 airline update", page)
        self.assertIn("CANCEL INVOICE · Kim Minji", page)
        self.assertIn("Invoice cancelled · stay not needed", page)
        self.assertIn("On time", page)
        dashboard = web_app.build_dashboard()
        self.assertEqual(dashboard["metrics"]["unpaid_revenue"], 0)
        self.assertEqual(dashboard["metrics"]["rooms_remaining"], dashboard["metrics"]["room_capacity"])
        self.assertEqual(self.csrf_post("/reset-demo").status_code, 302)
        self.assertEqual(common.notice_scenario(), "initial")

    def test_cancelled_frees_room_for_another_extension(self):
        self.save([self.extension("cancelled"),
                   {**self.extension("sent"), "id": "P002", "guest_id": "G001"},
                   {**self.extension("sent"), "id": "P003", "guest_id": "G004"}])
        result = agent.tool_propose_stay_extension({"guest_id": "G003", "nights": 3,
                                                    "reason": "Cancelled flight.", "message_draft": "A room is available."})
        self.assertTrue(result["accepted"])


if __name__ == "__main__":
    unittest.main()
