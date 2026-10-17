import json
import os
import re
import shutil
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import agent
import app as web_app
import approve
import check_payments
import common


ROOT = Path(__file__).resolve().parents[1]


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.data_dir = self.root / "data"
        self.log_dir = self.root / "logs"
        self.data_dir.mkdir()
        self.log_dir.mkdir()
        shutil.copy(ROOT / "data" / "guests.json", self.data_dir / "guests.json")
        shutil.copy(ROOT / "data" / "flights.json", self.data_dir / "flights.json")
        replacements = (
            (agent, "DATA_DIR", str(self.data_dir)),
            (common, "DATA_DIR", str(self.data_dir)),
            (common, "LOG_DIR", str(self.log_dir)),
            (web_app, "DATA_DIR", str(self.data_dir)),
            (web_app, "WORKSPACE_LOCKS", {}),
            (web_app, "ACTIVE_REQUESTS", {}),
            (web_app, "AI_SEMAPHORE", threading.BoundedSemaphore(2)),
        )
        self.patchers = [mock.patch.object(module, name, value) for module, name, value in replacements]
        self.patchers.extend([
            mock.patch("urllib.request.urlopen", side_effect=AssertionError("Network access is forbidden")),
            mock.patch.object(web_app, "load_env", return_value={}),
            mock.patch.object(approve, "load_env", return_value={"BUYER_EMAIL": "buyer@example.test"}),
        ])
        for patcher in self.patchers:
            patcher.start()
        web_app.app.config.update(TESTING=True, SECRET_KEY="workspace-test-secret")

    def tearDown(self):
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.temp_dir.cleanup()

    def _client(self):
        client = web_app.app.test_client()
        response = client.get("/")
        self.assertEqual(response.status_code, 200)
        with client.session_transaction() as session_data:
            workspace_id = session_data["workspace_id"]
            csrf_token = session_data["csrf_token"]
        self.assertRegex(workspace_id, r"^[0-9a-f]{32}$")
        return client, workspace_id, csrf_token

    def _queue_path(self, workspace_id):
        return self.data_dir / "workspaces" / workspace_id / "approvals.json"

    def _queue(self, workspace_id):
        path = self._queue_path(workspace_id)
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []

    def _post(self, client, path, csrf_token):
        return client.post(path, data={"csrf_token": csrf_token})

    @staticmethod
    def _fake_gemini(_env, contents):
        if len(contents) == 1:
            return {"candidates": [{"content": {"role": "model", "parts": [
                {"functionCall": {"name": "propose_stay_extension", "args": {
                    "guest_id": "G001", "nights": 2, "reason": "Official cancellation.",
                    "message_draft": "A safe extension is available.",
                }}},
                {"functionCall": {"name": "notify_staff", "args": {
                    "guest_id": "G004", "note": "Manual OTA follow-up required.",
                }}},
            ]}}]}
        return {"candidates": [{"content": {"role": "model", "parts": [{"text": "Done"}]}}]}

    @staticmethod
    def _pending(item_id="P001"):
        return {
            "id": item_id, "type": "extension", "guest_id": "G001", "guest_name": "Emily Carter",
            "language": "English", "nights": 2, "nightly_rate_usd": 80,
            "discount_percent": 20, "amount_usd": 128.0, "reason": "Official cancellation.",
            "message_draft": "Demo message.", "price_line": "System price.",
            "evidence": "Official notice", "status": "pending", "created_at": "2026-10-15T10:00:00",
        }

    def test_ai_results_are_private_to_each_client(self):
        a, a_id, a_csrf = self._client()
        b, b_id, _ = self._client()
        self.assertNotEqual(a_id, b_id)
        self.assertEqual(self._queue(b_id), [])
        with mock.patch.object(agent, "call_gemini", new=self._fake_gemini), mock.patch("builtins.print"):
            self.assertEqual(self._post(a, "/run-ai", a_csrf).status_code, 302)
        self.assertEqual(len(self._queue(a_id)), 2)
        self.assertEqual(self._queue(a_id)[0]["type"], "extension")
        self.assertEqual(self._queue(b_id), [])
        self.assertIn("Staff follow-up", a.get("/").get_data(as_text=True))
        self.assertNotIn("Manual OTA follow-up required.", b.get("/").get_data(as_text=True))

    def test_new_workspace_starts_with_five_guests_waiting_for_ai(self):
        client, workspace_id, _ = self._client()
        page = client.get("/").get_data(as_text=True)
        self.assertEqual(page.count("Awaiting AI review"), 5)
        self.assertEqual(self._queue(workspace_id), [])

    def test_reset_only_clears_its_own_workspace(self):
        a, a_id, a_csrf = self._client()
        b, b_id, b_csrf = self._client()
        self._queue_path(a_id).write_text(json.dumps([self._pending("A001")]), encoding="utf-8")
        self._queue_path(b_id).write_text(json.dumps([self._pending("B001")]), encoding="utf-8")
        self.assertEqual(self._post(a, "/reset-demo", a_csrf).status_code, 302)
        self.assertEqual(self._queue(a_id), [])
        self.assertEqual(self._queue(b_id)[0]["id"], "B001")
        self.assertEqual(len(list((self._queue_path(a_id).parent / "archive").iterdir())), 1)
        self.assertFalse((self._queue_path(b_id).parent / "archive").exists())
        self.assertEqual(self._post(b, "/items/B001/later", b_csrf).status_code, 302)

    def test_invalid_session_id_cannot_escape_workspace_directory(self):
        client, old_id, _ = self._client()
        with client.session_transaction() as session_data:
            session_data["workspace_id"] = "../../etc"
        response = client.get("/")
        with client.session_transaction() as session_data:
            new_id = session_data["workspace_id"]
        self.assertEqual(response.status_code, 200)
        self.assertNotEqual(old_id, new_id)
        self.assertTrue(re.fullmatch(r"[0-9a-f]{32}", new_id))
        self.assertTrue(self._queue_path(new_id).parent.is_dir())
        self.assertFalse((self.root / "etc").exists())

    def test_sixth_ai_run_is_blocked_before_model_call(self):
        client, workspace_id, csrf = self._client()
        answer = {"candidates": [{"content": {"role": "model", "parts": [{"text": "Done"}]}}]}
        with mock.patch.object(agent, "REPLAY_PATH", str(self.data_dir / "missing-replay.json")), \
             mock.patch.object(agent, "call_gemini", return_value=answer) as model, mock.patch("builtins.print"):
            for _ in range(6):
                self.assertEqual(self._post(client, "/run-ai", csrf).status_code, 302)
        self.assertEqual(model.call_count, 5)
        self.assertIn("AI demo limit for this hour", client.get("/").get_data(as_text=True))
        log = (self._queue_path(workspace_id).parent / "decision_log.jsonl").read_text(encoding="utf-8")
        self.assertIn('"action": "rate_limit_blocked"', log)

    def test_global_ai_limit_uses_replay_without_gemini_or_quota(self):
        client, workspace_id, csrf = self._client()
        replay_path = self.data_dir / "replay" / "gemini_run.json"
        replay_path.parent.mkdir()
        replay_path.write_text(json.dumps([
            {"candidates": [{"content": {"role": "model", "parts": [
                {"functionCall": {"name": "propose_stay_extension", "args": {
                    "guest_id": "G001", "nights": 2, "reason": "Official cancellation.",
                    "message_draft": "A safe extension is available.",
                }}},
            ]}}]},
            {"candidates": [{"content": {"role": "model", "parts": [{"text": "Done."}]}}]},
        ]), encoding="utf-8")

        with mock.patch.dict(os.environ, {"MAX_AI_RUNS_PER_DAY": "0"}), \
             mock.patch.object(agent, "REPLAY_PATH", str(replay_path)), \
             mock.patch.object(agent, "call_gemini", side_effect=AssertionError("Gemini must not be called")) as model, \
             mock.patch("builtins.print"):
            self.assertEqual(self._post(client, "/run-ai", csrf).status_code, 302)

        model.assert_not_called()
        self.assertEqual(len(self._queue(workspace_id)), 1)
        self.assertIn("Demo replay — recorded Gemini responses", client.get("/").get_data(as_text=True))
        self.assertFalse((self.data_dir / "workspaces" / "_limits.json").exists())
        log = (self._queue_path(workspace_id).parent / "decision_log.jsonl").read_text(encoding="utf-8")
        self.assertIn('"action": "replay_mode"', log)
        self.assertIn("global_day", log)

    def test_ai_run_without_a_model_call_does_not_use_a_slot(self):
        client, _, csrf = self._client()
        with mock.patch.object(agent, "run_agent", return_value=None):
            self.assertEqual(self._post(client, "/run-ai", csrf).status_code, 302)
        self.assertFalse((self.data_dir / "workspaces" / "_limits.json").exists())

    def test_global_ai_counter_resets_at_utc_midnight(self):
        before_midnight = datetime(2026, 10, 17, 23, 59, tzinfo=timezone.utc).timestamp()
        with mock.patch.dict(os.environ, {
            "MAX_AI_RUNS_PER_DAY": "1", "MAX_AI_RUNS_PER_HOUR_PER_VISITOR": "2",
        }):
            with mock.patch.object(web_app.time, "time", return_value=before_midnight):
                web_app._reserve_limit("ai", "a" * 32)
            with mock.patch.object(web_app.time, "time", return_value=before_midnight + 120):
                web_app._reserve_limit("ai", "a" * 32)
        limits = json.loads((self.data_dir / "workspaces" / "_limits.json").read_text(encoding="utf-8"))
        self.assertEqual(limits["day"], "2026-10-18")
        self.assertEqual(limits["ai_total"], 1)

    def test_global_invoice_limit_blocks_paypal_creation(self):
        a, a_id, a_csrf = self._client()
        b, b_id, b_csrf = self._client()
        self._queue_path(a_id).write_text(json.dumps([self._pending("A001")]), encoding="utf-8")
        self._queue_path(b_id).write_text(json.dumps([self._pending("B001")]), encoding="utf-8")
        with mock.patch.dict(os.environ, {"MAX_INVOICES_PER_DAY": "1"}), \
             mock.patch.object(approve, "create_extension_invoice", return_value=("INV-TEST", "128.00")) as create, \
             mock.patch.object(approve, "send_invoice", return_value=("SENT", "https://example.test/invoice")):
            self.assertEqual(self._post(a, "/proposals/A001/approve", a_csrf).status_code, 302)
            self.assertEqual(self._post(b, "/proposals/B001/approve", b_csrf).status_code, 302)
        create.assert_called_once()
        self.assertEqual(self._queue(a_id)[0]["status"], "sent")
        self.assertEqual(self._queue(b_id)[0]["status"], "pending")
        self.assertIn("invoice limit reached", b.get("/").get_data(as_text=True))

    def test_cannot_record_another_visitors_invoice(self):
        a, a_id, a_csrf = self._client()
        _b, b_id, _b_csrf = self._client()
        foreign = self._pending("P001")
        foreign.update(status="sent", invoice_id="INV-FOREIGN")
        self._queue_path(b_id).write_text(json.dumps([foreign]), encoding="utf-8")

        with mock.patch.object(web_app, "record_invoice_payment") as payment:
            self.assertEqual(self._post(a, "/proposals/P001/record-sandbox-payment", a_csrf).status_code, 302)
            self.assertEqual(self._post(a, "/proposals/INV-FOREIGN/record-sandbox-payment", a_csrf).status_code, 302)
        payment.assert_not_called()
        self.assertEqual(self._queue(a_id), [])
        self.assertEqual(self._queue(b_id)[0]["status"], "sent")

    def test_failed_invoice_creation_releases_reserved_slot(self):
        client, workspace_id, csrf = self._client()
        self._queue_path(workspace_id).write_text(json.dumps([self._pending()]), encoding="utf-8")
        with mock.patch.dict(os.environ, {"MAX_INVOICES_PER_DAY": "1"}), \
             mock.patch.object(approve, "create_extension_invoice", side_effect=[RuntimeError("mock failure"), ("INV-TEST", "128.00")]) as create, \
             mock.patch.object(approve, "send_invoice", return_value=("SENT", "https://example.test/invoice")):
            self._post(client, "/proposals/P001/approve", csrf)
            self.assertEqual(self._queue(workspace_id)[0]["status"], "pending")
            self._post(client, "/proposals/P001/approve", csrf)
        self.assertEqual(create.call_count, 2)
        self.assertEqual(self._queue(workspace_id)[0]["status"], "sent")

    def test_invoice_creation_counts_even_if_sending_fails(self):
        client, workspace_id, csrf = self._client()
        self._queue_path(workspace_id).write_text(
            json.dumps([self._pending("P001"), self._pending("P002")]), encoding="utf-8",
        )
        with mock.patch.dict(os.environ, {"MAX_INVOICES_PER_DAY_PER_VISITOR": "1"}), \
             mock.patch.object(approve, "create_extension_invoice", return_value=("INV-TEST", "128.00")) as create, \
             mock.patch.object(approve, "send_invoice", side_effect=RuntimeError("mock send failure")):
            self._post(client, "/proposals/P001/approve", csrf)
            self._post(client, "/proposals/P002/approve", csrf)
        create.assert_called_once()
        self.assertEqual([item["status"] for item in self._queue(workspace_id)], ["approved", "pending"])

    def test_third_concurrent_ai_run_is_rejected_without_waiting(self):
        clients = [self._client() for _ in range(3)]
        started = threading.Event()
        release = threading.Event()
        counter_lock = threading.Lock()
        calls = []
        outcomes = []
        answer = {"candidates": [{"content": {"role": "model", "parts": [{"text": "Done"}]}}]}

        def waiting_model(_env, _contents):
            with counter_lock:
                calls.append(1)
                if len(calls) == 2:
                    started.set()
            release.wait(5)
            return answer

        def run(client, csrf):
            outcomes.append(self._post(client, "/run-ai", csrf).status_code)

        with mock.patch.object(agent, "call_gemini", new=waiting_model), mock.patch("builtins.print"):
            workers = [threading.Thread(target=run, args=(client, csrf)) for client, _, csrf in clients[:2]]
            for worker in workers:
                worker.start()
            try:
                self.assertTrue(started.wait(3))
                start = time.monotonic()
                third_response = self._post(clients[2][0], "/run-ai", clients[2][2])
                self.assertLess(time.monotonic() - start, 1.0)
                self.assertEqual(third_response.status_code, 302)
            finally:
                release.set()
                for worker in workers:
                    worker.join(5)
        self.assertEqual(len(calls), 2)
        self.assertEqual(outcomes, [302, 302])
        self.assertIn("Other visitors are running the AI right now", clients[2][0].get("/").get_data(as_text=True))

    def test_reads_and_reset_do_not_wait_for_the_same_visitors_ai_run(self):
        a, a_id, a_csrf = self._client()
        a_second_tab = web_app.app.test_client()
        with a_second_tab.session_transaction() as session_data:
            session_data["workspace_id"] = a_id
            session_data["csrf_token"] = a_csrf
        b, _, _ = self._client()
        original = json.dumps([self._pending()])
        self._queue_path(a_id).write_text(original, encoding="utf-8")
        started = threading.Event()
        release = threading.Event()
        outcomes = []
        answer = {"candidates": [{"content": {"role": "model", "parts": [{"text": "Done"}]}}]}

        def waiting_model(_env, _contents):
            started.set()
            release.wait(5)
            return answer

        def run_ai():
            outcomes.append(self._post(a, "/run-ai", a_csrf).status_code)

        with mock.patch.object(agent, "call_gemini", new=waiting_model), mock.patch("builtins.print"):
            worker = threading.Thread(target=run_ai)
            worker.start()
            try:
                self.assertTrue(started.wait(3))
                for client, path in ((a_second_tab, "/"), (b, "/"), (b, "/healthz")):
                    start = time.monotonic()
                    self.assertEqual(client.get(path).status_code, 200)
                    self.assertLess(time.monotonic() - start, 1.0)
                start = time.monotonic()
                response = self._post(a_second_tab, "/reset-demo", a_csrf)
                self.assertEqual(response.status_code, 302)
                self.assertLess(time.monotonic() - start, 1.0)
                self.assertEqual(self._queue_path(a_id).read_text(encoding="utf-8"), original)
                self.assertFalse((self._queue_path(a_id).parent / "archive").exists())
                self.assertIn(
                    "Your previous action is still running. Please wait a moment and refresh.",
                    a_second_tab.get("/").get_data(as_text=True),
                )
                entries = [json.loads(line) for line in (self._queue_path(a_id).parent / "decision_log.jsonl").read_text(encoding="utf-8").splitlines()]
                self.assertIn({"action": "reset_demo"}, [entry["detail"] for entry in entries if entry["action"] == "workspace_busy"])
                limits = json.loads((self.data_dir / "workspaces" / "_limits.json").read_text(encoding="utf-8"))
                self.assertEqual(limits["ai_total"], 1)
            finally:
                release.set()
                worker.join(5)
        self.assertEqual(outcomes, [302])

    def test_every_post_action_rejects_a_busy_workspace(self):
        client, workspace_id, csrf = self._client()
        lock = web_app.WORKSPACE_LOCKS[workspace_id]
        actions = {
            "/run-ai": "run_ai_check",
            "/reset-demo": "reset_demo",
            "/proposals/P001/approve": "approve_proposal",
            "/proposals/P001/reject": "reject_proposal",
            "/items/P001/later": "review_later",
            "/staff-notes/P001/handle": "handle_staff_note",
            "/check-payments": "check_payments",
            "/proposals/P001/record-sandbox-payment": "record_sandbox_payment",
        }
        lock.acquire()
        try:
            for path in actions:
                start = time.monotonic()
                self.assertEqual(self._post(client, path, csrf).status_code, 302)
                self.assertLess(time.monotonic() - start, 1.0)
        finally:
            lock.release()
        log_path = self._queue_path(workspace_id).parent / "decision_log.jsonl"
        busy_actions = [
            json.loads(line)["detail"]["action"] for line in log_path.read_text(encoding="utf-8").splitlines()
            if json.loads(line)["action"] == "workspace_busy"
        ]
        self.assertEqual(busy_actions, list(actions.values()))
        self.assertFalse((self.data_dir / "workspaces" / "_limits.json").exists())

    def test_inactive_workspace_is_removed_after_24_hours(self):
        root = self.data_dir / "workspaces"
        old = root / ("b" * 32)
        old.mkdir(parents=True)
        limits = root / "_limits.json"
        limits.write_text("{}", encoding="utf-8")
        current = time.time()
        os.utime(old, (current - 86401, current - 86401))
        with mock.patch.object(web_app.time, "time", return_value=current):
            web_app.cleanup_workspaces()
        self.assertFalse(old.exists())
        self.assertTrue(limits.exists())

    def test_cleanup_removes_only_deleted_visitors_from_limits(self):
        root = self.data_dir / "workspaces"
        old_id, active_id = "b" * 32, "c" * 32
        old = root / old_id
        active = root / active_id
        old.mkdir(parents=True)
        active.mkdir()
        limits_path = root / "_limits.json"
        limits_path.write_text(json.dumps({
            "day": "2026-10-17", "ai_total": 2, "invoice_total": 1,
            "visitors": {
                old_id: {"ai_times": [], "invoice_total": 1},
                active_id: {"ai_times": [], "invoice_total": 0},
            },
        }), encoding="utf-8")
        current = time.time()
        os.utime(old, (current - 86401, current - 86401))
        with mock.patch.object(web_app.time, "time", return_value=current):
            web_app.cleanup_workspaces()
        limits = json.loads(limits_path.read_text(encoding="utf-8"))
        self.assertFalse(old.exists())
        self.assertTrue(active.exists())
        self.assertNotIn(old_id, limits["visitors"])
        self.assertIn(active_id, limits["visitors"])
        self.assertEqual((limits["ai_total"], limits["invoice_total"]), (2, 1))

    def test_new_utc_day_drops_empty_visitor_counters(self):
        root = self.data_dir / "workspaces"
        root.mkdir()
        old_id, recent_id = "b" * 32, "c" * 32
        now = datetime(2026, 10, 18, 0, 1, tzinfo=timezone.utc).timestamp()
        (root / "_limits.json").write_text(json.dumps({
            "day": "2026-10-17", "ai_total": 4, "invoice_total": 2,
            "visitors": {
                old_id: {"ai_times": [], "invoice_total": 2},
                recent_id: {"ai_times": [now - 60], "invoice_total": 0},
            },
        }), encoding="utf-8")
        with mock.patch.object(web_app.time, "time", return_value=now):
            web_app._reserve_limit("ai", recent_id)
        limits = json.loads((root / "_limits.json").read_text(encoding="utf-8"))
        self.assertNotIn(old_id, limits["visitors"])
        self.assertIn(recent_id, limits["visitors"])
        self.assertEqual(limits["invoice_total"], 0)

    def test_failed_atomic_json_write_preserves_original_and_removes_temp_file(self):
        path = self.root / "state.json"
        original = '{"status":"original"}'
        path.write_text(original, encoding="utf-8")

        def fail_after_partial_write(_data, output, **_kwargs):
            output.write('{"status":')
            raise RuntimeError("mock write failure")

        with mock.patch.object(common.json, "dump", side_effect=fail_after_partial_write):
            with self.assertRaises(RuntimeError):
                common.write_json(str(path), {"status": "new"})
        self.assertEqual(path.read_text(encoding="utf-8"), original)
        self.assertEqual([item.name for item in self.root.iterdir() if item.is_file()], ["state.json"])

    def test_returning_after_24_hours_receives_a_new_workspace(self):
        client, old_id, _ = self._client()
        old = self._queue_path(old_id).parent
        current = time.time()
        os.utime(old, (current - 86401, current - 86401))
        with mock.patch.object(web_app.time, "time", return_value=current):
            self.assertEqual(client.get("/").status_code, 200)
        with client.session_transaction() as session_data:
            new_id = session_data["workspace_id"]
        self.assertNotEqual(new_id, old_id)
        self.assertFalse(old.exists())

    def test_workspace_count_is_capped_at_500(self):
        root = self.data_dir / "workspaces"
        root.mkdir()
        current = time.time()
        for number in range(501):
            directory = root / f"{number:032x}"
            directory.mkdir()
            os.utime(directory, (current - 501 + number, current - 501 + number))
        web_app.cleanup_workspaces(now=current)
        self.assertEqual(sum(1 for item in root.iterdir() if item.is_dir()), 500)
        self.assertFalse((root / f"{0:032x}").exists())

    def test_new_visitor_does_not_raise_workspace_count_above_500(self):
        root = self.data_dir / "workspaces"
        root.mkdir()
        current = time.time()
        for number in range(500):
            directory = root / f"{number:032x}"
            directory.mkdir()
            os.utime(directory, (current - 500 + number, current - 500 + number))
        _, new_id, _ = self._client()
        self.assertEqual(sum(1 for item in root.iterdir() if item.is_dir()), 500)
        self.assertTrue((root / new_id).is_dir())
        self.assertFalse((root / f"{0:032x}").exists())

    def test_cli_defaults_to_original_approvals_and_log_paths(self):
        approvals = self.data_dir / "approvals.json"
        log = self.log_dir / "decision_log.jsonl"
        self.assertEqual(common.approvals_path(), str(approvals))
        self.assertEqual(common.log_path(), str(log))
        agent.tool_notify_staff({"guest_id": "G004", "note": "CLI demo note."})
        queue = json.loads(approvals.read_text(encoding="utf-8"))
        self.assertEqual(queue[0]["type"], "staff_note")
        pending = self._pending("P002")
        sent = self._pending("P003")
        sent.update(status="sent", invoice_id="INV-CLI")
        approvals.write_text(json.dumps([queue[0], pending, sent]), encoding="utf-8")
        approve.reject_extension("P002")
        with mock.patch.object(check_payments, "_get_token", return_value="test-token"), \
             mock.patch.object(check_payments, "get_invoice_status", return_value="PAID"):
            check_payments.check_payments(env={"PAYPAL_CLIENT_ID": "test-client"})
        statuses = [item["status"] for item in json.loads(approvals.read_text(encoding="utf-8"))]
        self.assertEqual(statuses, ["pending", "rejected", "confirmed"])
        entries = [json.loads(line)["action"] for line in log.read_text(encoding="utf-8").splitlines()]
        self.assertIn("notified_staff", entries)
        self.assertIn("rejected_extension", entries)
        self.assertIn("payment_confirmed", entries)


if __name__ == "__main__":
    unittest.main()
