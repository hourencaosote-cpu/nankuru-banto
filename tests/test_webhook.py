import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import app as web_app
import approve
import check_payments
import common
import paypal_tools


ROOT = Path(__file__).resolve().parents[1]


class PayPalWebhookTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.data_dir = self.root / "data"
        self.log_dir = self.root / "logs"
        self.workspace_id = "a" * 32
        self.workspace_dir = self.data_dir / "workspaces" / self.workspace_id
        self.workspace_dir.mkdir(parents=True)
        self.log_dir.mkdir()
        shutil.copy(ROOT / "data" / "guests.json", self.data_dir / "guests.json")
        shutil.copy(ROOT / "data" / "flights.json", self.data_dir / "flights.json")
        self.queue_path = self.workspace_dir / "approvals.json"
        self.workspace_log = self.workspace_dir / "decision_log.jsonl"
        self.invoice_id = "INV-WEBHOOK-1"
        self._write_queue([self._item()])

        replacements = (
            (common, "DATA_DIR", str(self.data_dir)),
            (common, "LOG_DIR", str(self.log_dir)),
            (web_app, "DATA_DIR", str(self.data_dir)),
            (web_app, "WORKSPACE_LOCKS", {}),
            (web_app, "ACTIVE_REQUESTS", {}),
        )
        self.patchers = [mock.patch.object(module, name, value) for module, name, value in replacements]
        self.patchers.extend([
            mock.patch.object(web_app, "load_env", return_value={"PAYPAL_WEBHOOK_ID": "WH-TEST"}),
            mock.patch("urllib.request.urlopen", side_effect=AssertionError("Network access is forbidden")),
        ])
        for patcher in self.patchers:
            patcher.start()
        web_app.app.config.update(TESTING=True, SECRET_KEY="webhook-test-secret")
        self.client = web_app.app.test_client()
        with self.client.session_transaction() as session_data:
            session_data["workspace_id"] = self.workspace_id
        web_app._remember_invoice(self.invoice_id, self.workspace_id)

    def tearDown(self):
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.temp_dir.cleanup()

    def _item(self):
        return {
            "id": "P001", "type": "extension", "guest_id": "G001", "guest_name": "Emily Carter",
            "status": "sent", "invoice_id": self.invoice_id, "nights": 2, "amount_usd": 128.0,
            "nightly_rate_usd": 80, "discount_percent": 20,
            "message_draft": "Demo message.", "price_line": "System price.",
        }

    def _write_queue(self, items):
        self.queue_path.write_text(json.dumps(items), encoding="utf-8")

    def _queue(self):
        return json.loads(self.queue_path.read_text(encoding="utf-8"))

    def _event(self, invoice_id=None, event_type="INVOICING.INVOICE.PAID"):
        return {
            "event_type": event_type,
            "resource": {
                "invoice": {"id": invoice_id or self.invoice_id, "status": "PAID"},
            },
        }

    def _entries(self, path):
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def test_signature_verification_uses_paypal_postback_fields(self):
        headers = {
            "PAYPAL-AUTH-ALGO": "SHA256withRSA",
            "PAYPAL-CERT-URL": "https://api-m.sandbox.paypal.com/cert",
            "PAYPAL-TRANSMISSION-ID": "TX-1",
            "PAYPAL-TRANSMISSION-SIG": "signature",
            "PAYPAL-TRANSMISSION-TIME": "2026-10-27T00:00:00Z",
        }
        event = self._event()
        with mock.patch.object(paypal_tools, "_get_token", return_value="test-token"), \
             mock.patch.object(paypal_tools, "_call", return_value=(200, {"verification_status": "SUCCESS"})) as call:
            self.assertTrue(paypal_tools.verify_webhook_signature({"PAYPAL_WEBHOOK_ID": "WH-TEST"}, headers, event))
        self.assertEqual(call.call_args.args, ("POST", "/v1/notifications/verify-webhook-signature"))
        self.assertEqual(call.call_args.kwargs["token"], "test-token")
        self.assertEqual(call.call_args.kwargs["body"], {
            "auth_algo": "SHA256withRSA", "cert_url": headers["PAYPAL-CERT-URL"],
            "transmission_id": "TX-1", "transmission_sig": "signature",
            "transmission_time": "2026-10-27T00:00:00Z", "webhook_id": "WH-TEST",
            "webhook_event": event,
        })
        self.assertEqual(paypal_tools.API_BASE, "https://api-m.sandbox.paypal.com")

        with mock.patch.object(paypal_tools, "_get_token", return_value="test-token"), \
             mock.patch.object(paypal_tools, "_call", return_value=(200, {"verification_status": "FAILURE"})):
            self.assertFalse(paypal_tools.verify_webhook_signature({"PAYPAL_WEBHOOK_ID": "WH-TEST"}, headers, event))

    def test_verified_paid_invoice_confirms_its_workspace(self):
        other_id = "b" * 32
        other_dir = self.data_dir / "workspaces" / other_id
        other_dir.mkdir()
        other_queue = other_dir / "approvals.json"
        other_queue.write_text(json.dumps([{**self._item(), "invoice_id": "INV-OTHER"}]), encoding="utf-8")
        anonymous = web_app.app.test_client()
        with mock.patch.object(web_app, "verify_webhook_signature", return_value=True), \
             mock.patch.object(check_payments, "get_invoice_status", return_value="PAID") as get_status:
            response = anonymous.post("/webhooks/paypal", json=self._event())
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("Set-Cookie", response.headers)
        get_status.assert_called_once()
        self.assertEqual(self._queue()[0]["status"], "confirmed")
        self.assertEqual(json.loads(other_queue.read_text(encoding="utf-8"))[0]["status"], "sent")
        entries = self._entries(self.workspace_log)
        self.assertEqual([entry["detail"]["via"] for entry in entries
                          if entry["action"] == "payment_confirmed"], ["webhook"])
        self.assertEqual(
            [entry["detail"] for entry in entries if entry["action"] == "webhook_received"],
            [{"invoice_id": self.invoice_id, "result": "confirmed"}],
        )
        page = self.client.get("/").get_data(as_text=True)
        self.assertIn("Payment confirmed by PayPal webhook", page)
        self.assertIn("Check payments now", page)

    def test_failed_signature_changes_nothing(self):
        original = self.queue_path.read_bytes()
        with mock.patch.object(web_app, "verify_webhook_signature", return_value=False), \
             mock.patch.object(check_payments, "get_invoice_status") as get_status:
            response = self.client.post("/webhooks/paypal", json=self._event())
        self.assertEqual(response.status_code, 400)
        get_status.assert_not_called()
        self.assertEqual(self.queue_path.read_bytes(), original)
        self.assertEqual(self._entries(self.workspace_log), [])

    def test_legacy_resource_id_still_confirms_invoice(self):
        event = {"event_type": "INVOICING.INVOICE.PAID",
                 "resource": {"id": self.invoice_id, "status": "PAID"}}
        with mock.patch.object(web_app, "verify_webhook_signature", return_value=True), \
             mock.patch.object(check_payments, "get_invoice_status", return_value="PAID") as get_status:
            response = self.client.post("/webhooks/paypal", json=event)
        self.assertEqual(response.status_code, 200)
        get_status.assert_called_once()
        self.assertEqual(self._queue()[0]["status"], "confirmed")

    def test_nested_invoice_id_takes_priority_over_resource_id(self):
        event = self._event()
        event["resource"]["id"] = "EVENT-RESOURCE-ID"
        with mock.patch.object(web_app, "verify_webhook_signature", return_value=True), \
             mock.patch.object(check_payments, "get_invoice_status", return_value="PAID") as get_status:
            response = self.client.post("/webhooks/paypal", json=event)
        self.assertEqual(response.status_code, 200)
        get_status.assert_called_once()
        self.assertEqual(self._queue()[0]["status"], "confirmed")

    def test_unpaid_status_after_three_checks_requests_retry(self):
        with mock.patch.object(web_app, "verify_webhook_signature", return_value=True), \
             mock.patch.object(check_payments, "get_invoice_status", return_value="SENT") as get_status, \
             mock.patch.object(web_app.time, "sleep") as sleep:
            response = self.client.post("/webhooks/paypal", json=self._event())
        self.assertEqual(response.status_code, 503)
        self.assertEqual(get_status.call_count, 3)
        self.assertEqual(sleep.call_args_list, [mock.call(2), mock.call(2)])
        self.assertEqual(self._queue()[0]["status"], "sent")
        self.assertEqual(
            [entry["detail"] for entry in self._entries(self.workspace_log)
             if entry["action"] == "webhook_received"],
            [{"invoice_id": self.invoice_id, "result": "not_paid_yet"}],
        )

    def test_second_check_can_confirm_after_initial_sent_status(self):
        with mock.patch.object(web_app, "verify_webhook_signature", return_value=True), \
             mock.patch.object(check_payments, "get_invoice_status", side_effect=["SENT", "PAID"]) as get_status, \
             mock.patch.object(web_app.time, "sleep") as sleep:
            response = self.client.post("/webhooks/paypal", json=self._event())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(get_status.call_count, 2)
        sleep.assert_called_once_with(2)
        self.assertEqual(self._queue()[0]["status"], "confirmed")
        self.assertEqual(
            [entry["detail"] for entry in self._entries(self.workspace_log)
             if entry["action"] == "webhook_received"],
            [{"invoice_id": self.invoice_id, "result": "confirmed"}],
        )

    def test_duplicate_webhook_confirms_and_logs_once(self):
        with mock.patch.object(web_app, "verify_webhook_signature", return_value=True), \
             mock.patch.object(check_payments, "get_invoice_status", return_value="MARKED_AS_PAID") as get_status:
            self.assertEqual(self.client.post("/webhooks/paypal", json=self._event()).status_code, 200)
            self.assertEqual(self.client.post("/webhooks/paypal", json=self._event()).status_code, 200)
        get_status.assert_called_once()
        entries = self._entries(self.workspace_log)
        self.assertEqual(sum(entry["action"] == "payment_confirmed" for entry in entries), 1)
        self.assertEqual(
            [entry["detail"] for entry in entries if entry["action"] == "webhook_received"],
            [{"invoice_id": self.invoice_id, "result": "confirmed"},
             {"invoice_id": self.invoice_id, "result": "already_confirmed"}],
        )

    def test_cancelled_webhook_requires_signature_and_get_and_is_idempotent(self):
        event = self._event(event_type="INVOICING.INVOICE.CANCELLED")
        event["resource"]["invoice"]["status"] = "CANCELLED"
        with mock.patch.object(web_app, "verify_webhook_signature", return_value=False), \
             mock.patch.object(web_app, "get_invoice_status") as get_status:
            self.assertEqual(self.client.post("/webhooks/paypal", json=event).status_code, 400)
            get_status.assert_not_called()
        with mock.patch.object(web_app, "verify_webhook_signature", return_value=True), \
             mock.patch.object(web_app, "get_invoice_status", return_value="SENT"):
            self.assertEqual(self.client.post("/webhooks/paypal", json=event).status_code, 503)
        self.assertEqual(self._queue()[0]["status"], "sent")
        with mock.patch.object(web_app, "verify_webhook_signature", return_value=True), \
             mock.patch.object(web_app, "get_invoice_status", return_value="CANCELLED") as get_status:
            self.assertEqual(self.client.post("/webhooks/paypal", json=event).status_code, 200)
            self.assertEqual(self.client.post("/webhooks/paypal", json=event).status_code, 200)
        self.assertEqual(get_status.call_count, 2)
        self.assertEqual(self._queue()[0]["status"], "cancelled")
        entries = [entry for entry in self._entries(self.workspace_log)
                   if entry["action"] == "invoice_cancel_confirmed"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["detail"]["guest_id"], "G001")
        self.assertEqual(entries[0]["detail"]["via"], "webhook")

    def test_cancelled_webhook_closes_pending_cancellation_in_same_workspace(self):
        cancellation = {"id": "P002", "type": "invoice_cancellation", "proposal_id": "P001",
                        "guest_id": "G001", "status": "pending"}
        self._write_queue([self._item(), cancellation])
        event = self._event(event_type="INVOICING.INVOICE.CANCELLED")
        event["resource"]["invoice"]["status"] = "CANCELLED"
        with mock.patch.object(web_app, "verify_webhook_signature", return_value=True), \
             mock.patch.object(web_app, "get_invoice_status", return_value="CANCELLED"):
            self.assertEqual(self.client.post("/webhooks/paypal", json=event).status_code, 200)
            self.assertEqual(self.client.post("/webhooks/paypal", json=event).status_code, 200)
        self.assertEqual([item["status"] for item in self._queue()], ["cancelled", "done"])
        entries = [entry for entry in self._entries(self.workspace_log)
                   if entry["action"] == "invoice_cancel_confirmed"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["detail"]["proposal_closed"], "P002")

    def test_cancelled_webhook_closes_leftover_proposal_for_already_cancelled_extension(self):
        self._write_queue([
            {**self._item(), "status": "cancelled"},
            {"id": "P002", "type": "invoice_cancellation", "proposal_id": "P001",
             "guest_id": "G001", "status": "pending"},
            {"id": "P003", "type": "invoice_cancellation", "proposal_id": "P999",
             "guest_id": "G002", "status": "pending"},
        ])
        event = self._event(event_type="INVOICING.INVOICE.CANCELLED")
        with mock.patch.object(web_app, "verify_webhook_signature", return_value=True), \
             mock.patch.object(web_app, "get_invoice_status", return_value="CANCELLED"):
            self.assertEqual(self.client.post("/webhooks/paypal", json=event).status_code, 200)
        self.assertEqual([item["status"] for item in self._queue()], ["cancelled", "done", "pending"])
        entries = [entry for entry in self._entries(self.workspace_log)
                   if entry["action"] == "invoice_cancel_confirmed"]
        self.assertEqual(entries[0]["detail"]["proposal_closed"], "P002")

    def test_cancelled_webhook_accepts_legacy_resource_id(self):
        event = {"event_type": "INVOICING.INVOICE.CANCELLED",
                 "resource": {"id": self.invoice_id, "status": "CANCELLED"}}
        with mock.patch.object(web_app, "verify_webhook_signature", return_value=True), \
             mock.patch.object(web_app, "get_invoice_status", return_value="CANCELLED"):
            self.assertEqual(self.client.post("/webhooks/paypal", json=event).status_code, 200)
        self.assertEqual(self._queue()[0]["status"], "cancelled")

    def test_unknown_invoice_and_other_event_are_ignored(self):
        with mock.patch.object(web_app, "verify_webhook_signature", return_value=True), \
             mock.patch.object(check_payments, "get_invoice_status") as get_status:
            unknown = self.client.post("/webhooks/paypal", json=self._event(invoice_id="INV-UNKNOWN"))
            other = self.client.post("/webhooks/paypal", json=self._event(event_type="INVOICING.INVOICE.SENT"))
        self.assertEqual((unknown.status_code, other.status_code), (200, 200))
        get_status.assert_not_called()
        self.assertEqual(self._queue()[0]["status"], "sent")
        self.assertEqual(self._entries(self.workspace_log), [])

    def test_missing_webhook_id_returns_503_and_logs_only_action(self):
        with mock.patch.object(web_app, "load_env", return_value={}), \
             mock.patch.object(web_app, "verify_webhook_signature") as verify:
            response = self.client.post("/webhooks/paypal", json=self._event())
        self.assertEqual(response.status_code, 503)
        verify.assert_not_called()
        self.assertEqual(self._entries(self.log_dir / "decision_log.jsonl")[0]["action"],
                         "webhook_not_configured")

    def test_csrf_exemption_does_not_exempt_other_posts(self):
        with mock.patch.object(web_app, "verify_webhook_signature", return_value=True), \
             mock.patch.object(check_payments, "get_invoice_status", return_value="PAID"):
            self.assertEqual(self.client.post("/webhooks/paypal", json=self._event()).status_code, 200)
        self.assertEqual(self.client.post("/check-payments").status_code, 400)

    def test_webhook_without_cookie_does_not_create_workspace(self):
        before = list((self.data_dir / "workspaces").iterdir())
        anonymous = web_app.app.test_client()
        with mock.patch.object(web_app, "verify_webhook_signature", return_value=True):
            response = anonymous.post("/webhooks/paypal", json=self._event(invoice_id="INV-UNKNOWN"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(list((self.data_dir / "workspaces").iterdir()), before)
        self.assertNotIn("Set-Cookie", response.headers)

    def test_busy_workspace_returns_503_without_change(self):
        busy_lock = mock.Mock()
        busy_lock.acquire.return_value = False
        web_app.WORKSPACE_LOCKS[self.workspace_id] = busy_lock
        with mock.patch.object(web_app, "verify_webhook_signature", return_value=True), \
             mock.patch.object(check_payments, "get_invoice_status") as get_status:
            response = self.client.post("/webhooks/paypal", json=self._event())
        self.assertEqual(response.status_code, 503)
        busy_lock.acquire.assert_called_once_with(timeout=10)
        busy_lock.release.assert_not_called()
        get_status.assert_not_called()
        self.assertEqual(web_app.ACTIVE_REQUESTS, {})
        self.assertEqual(self._queue()[0]["status"], "sent")

    def test_api_state_is_read_only_and_version_changes_with_status(self):
        original = self.queue_path.read_bytes()
        modified = self.workspace_dir.stat().st_mtime_ns
        first = self.client.get("/api/state")
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json["sent"], 1)
        self.assertEqual(self.queue_path.read_bytes(), original)
        self.assertEqual(self.workspace_dir.stat().st_mtime_ns, modified)
        changed = self._queue()
        changed[0]["status"] = "confirmed"
        self._write_queue(changed)
        second = self.client.get("/api/state")
        self.assertEqual(second.json["sent"], 0)
        self.assertNotEqual(first.json["version"], second.json["version"])
        self.assertEqual(self._entries(self.workspace_log), [])

    def test_invoice_index_is_written_on_web_approval_and_cleaned_with_workspace(self):
        pending = self._item()
        pending["status"] = "pending"
        pending.pop("invoice_id")
        self._write_queue([pending])
        page = self.client.get("/")
        self.assertEqual(page.status_code, 200)
        with self.client.session_transaction() as session_data:
            csrf = session_data["csrf_token"]
        with mock.patch.object(approve, "load_env", return_value={"BUYER_EMAIL": "buyer@example.test"}), \
             mock.patch.object(approve, "create_extension_invoice", return_value=("INV-NEW", "128.00")), \
             mock.patch.object(approve, "send_invoice", return_value=("SENT", "")):
            response = self.client.post("/proposals/P001/approve", data={"csrf_token": csrf})
        self.assertEqual(response.status_code, 302)
        index_path = self.data_dir / "workspaces" / "_invoice_index.json"
        self.assertEqual(json.loads(index_path.read_text(encoding="utf-8"))["INV-NEW"], self.workspace_id)

        old_id = "b" * 32
        old_dir = self.data_dir / "workspaces" / old_id
        old_dir.mkdir()
        web_app._remember_invoice("INV-OLD", old_id)
        now = time.time()
        os.utime(old_dir, (now - web_app.WORKSPACE_TTL_SECONDS - 1,) * 2)
        web_app.cleanup_workspaces(now=now)
        index = json.loads(index_path.read_text(encoding="utf-8"))
        self.assertNotIn("INV-OLD", index)
        self.assertEqual(index["INV-NEW"], self.workspace_id)

    def test_invoice_index_is_written_even_when_sending_fails(self):
        pending = self._item()
        pending["status"] = "pending"
        pending.pop("invoice_id")
        self._write_queue([pending])
        self.client.get("/")
        with self.client.session_transaction() as session_data:
            csrf = session_data["csrf_token"]
        with mock.patch.object(approve, "load_env", return_value={"BUYER_EMAIL": "buyer@example.test"}), \
             mock.patch.object(approve, "create_extension_invoice", return_value=("INV-DRAFT", "128.00")), \
             mock.patch.object(approve, "send_invoice", side_effect=RuntimeError("mock send failure")):
            response = self.client.post("/proposals/P001/approve", data={"csrf_token": csrf})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self._queue()[0]["status"], "approved")
        index = json.loads((self.data_dir / "workspaces" / "_invoice_index.json").read_text(encoding="utf-8"))
        self.assertEqual(index["INV-DRAFT"], self.workspace_id)

    def test_invoice_index_failure_is_logged_but_invoice_is_sent(self):
        pending = self._item()
        pending["status"] = "pending"
        pending.pop("invoice_id")
        self._write_queue([pending])
        self.client.get("/")
        with self.client.session_transaction() as session_data:
            csrf = session_data["csrf_token"]
        with mock.patch.object(approve, "load_env", return_value={"BUYER_EMAIL": "buyer@example.test"}), \
             mock.patch.object(approve, "create_extension_invoice", return_value=("INV-UNINDEXED", "128.00")), \
             mock.patch.object(web_app, "_remember_invoice", side_effect=OSError("private detail")), \
             mock.patch.object(approve, "send_invoice", return_value=("SENT", "")) as send:
            response = self.client.post("/proposals/P001/approve", data={"csrf_token": csrf})

        self.assertEqual(response.status_code, 302)
        send.assert_called_once()
        self.assertEqual(self._queue()[0]["status"], "sent")
        failures = [entry["detail"] for entry in self._entries(self.workspace_log)
                    if entry["action"] == "web_action_failed"]
        self.assertEqual(failures, [{"action": "invoice_index", "error_type": "OSError"}])
        self.assertNotIn("private detail", self.workspace_log.read_text(encoding="utf-8"))
        page = self.client.get("/").get_data(as_text=True)
        self.assertIn(
            "Approved. The PayPal sandbox invoice was sent. "
            "Automatic payment updates are unavailable for this invoice. Use Check payments now.",
            page,
        )

    def test_cli_approval_does_not_add_to_web_invoice_index(self):
        cli_queue = self.data_dir / "approvals.json"
        pending = self._item()
        pending["status"] = "pending"
        pending.pop("invoice_id")
        cli_queue.write_text(json.dumps([pending]), encoding="utf-8")
        index_path = self.data_dir / "workspaces" / "_invoice_index.json"
        original_index = index_path.read_bytes()
        with mock.patch.object(approve, "load_env", return_value={}), \
             mock.patch.object(approve, "create_extension_invoice", return_value=("INV-CLI", "128.00")):
            approve.approve_extension("P001")
        self.assertEqual(json.loads(cli_queue.read_text(encoding="utf-8"))[0]["status"], "approved")
        self.assertEqual(index_path.read_bytes(), original_index)

    def test_webhook_limit_is_64kb_and_other_posts_remain_16kb(self):
        oversized = self._event()
        oversized["padding"] = "x" * (64 * 1024)
        response = self.client.post("/webhooks/paypal", json=oversized)
        self.assertEqual(response.status_code, 413)
        other = self.client.post("/check-payments", data={"padding": "x" * (17 * 1024)})
        self.assertEqual(other.status_code, 413)

    def test_webhook_failure_logs_only_exception_type(self):
        with mock.patch.object(web_app, "verify_webhook_signature", side_effect=RuntimeError("private value")):
            response = self.client.post("/webhooks/paypal", json=self._event())
        self.assertEqual(response.status_code, 503)
        log = (self.log_dir / "decision_log.jsonl").read_text(encoding="utf-8")
        self.assertNotIn("private value", log)
        self.assertEqual(self._entries(self.log_dir / "decision_log.jsonl")[0]["detail"],
                         {"error_type": "RuntimeError"})

    def test_manual_payment_check_uses_shared_confirmation_and_via_label(self):
        tokens = common.set_workspace_paths(str(self.workspace_dir))
        try:
            with mock.patch.object(check_payments, "_get_token", return_value="test-token"), \
                 mock.patch.object(check_payments, "get_invoice_status", return_value="PAID"):
                result = check_payments.check_payments(env={"PAYPAL_CLIENT_ID": "test"})
        finally:
            common.reset_workspace_paths(tokens)
        self.assertEqual(len(result["newly_confirmed"]), 1)
        entry = self._entries(self.workspace_log)[0]
        self.assertEqual(entry["detail"]["via"], "check_payments")


if __name__ == "__main__":
    unittest.main()
