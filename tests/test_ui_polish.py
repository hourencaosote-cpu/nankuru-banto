import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app as web_app
import common


ROOT = Path(__file__).resolve().parents[1]


class UiPolishTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.data = Path(self.temp.name) / "data"
        self.workspace = self.data / "workspaces" / ("d" * 32)
        self.workspace.mkdir(parents=True)
        for name in ("guests.json", "flights.json", "flights_update.json"):
            shutil.copy(ROOT / "data" / name, self.data / name)
        self.queue_path = self.workspace / "approvals.json"
        self.queue_path.write_text("[]", encoding="utf-8")
        self.patchers = [
            mock.patch.object(common, "DATA_DIR", str(self.data)),
            mock.patch.object(web_app, "DATA_DIR", str(self.data)),
            mock.patch("urllib.request.urlopen", side_effect=AssertionError("Network forbidden")),
        ]
        for patcher in self.patchers:
            patcher.start()
        self.tokens = common.set_workspace_paths(str(self.workspace))
        web_app.app.config.update(TESTING=True, SECRET_KEY="test-ui-polish")
        self.client = web_app.app.test_client()
        with self.client.session_transaction() as state:
            state["workspace_id"] = "d" * 32

    def tearDown(self):
        common.reset_workspace_paths(self.tokens)
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.temp.cleanup()

    def save(self, items):
        common.write_json(str(self.queue_path), items)

    def extension(self, item_id, guest_id, status, created_at):
        names = {"G001": ("Emily Carter", "English", 80, 2),
                 "G002": ("Kim Minji", "Korean", 70, 1),
                 "G003": ("Ken Tanaka (田中 健)", "Japanese", 60, 3)}
        name, language, rate, nights = names[guest_id]
        return {"id": item_id, "type": "extension", "guest_id": guest_id,
                "guest_name": name, "language": language, "nights": nights,
                "nightly_rate_usd": rate, "discount_percent": 20,
                "amount_usd": round(rate * nights * 0.8, 2),
                "message_draft": "A guest message", "reason": "Official cancellation",
                "evidence": "Official notice at proposal", "invoice_id": f"INV-{item_id}",
                "status": status, "created_at": created_at}

    def related(self, item_id, kind, proposal_id, guest_id, status, created_at, **extra):
        names = {"G001": "Emily Carter", "G002": "Kim Minji", "G003": "Ken Tanaka (田中 健)"}
        return {"id": item_id, "type": kind, "proposal_id": proposal_id,
                "guest_id": guest_id, "guest_name": names[guest_id], "language": "English",
                "message_draft": "A guest message", "reason": "Official update",
                "evidence": "Official notice", "amount_usd": 144,
                "status": status, "created_at": created_at, **extra}

    def tracker_status(self, guest_id):
        dashboard = web_app.build_dashboard()
        guest_name = next(row["name"] for row in dashboard["guests"] if row["id"] == guest_id)
        row = next(row for row in dashboard["tracker_data"]["rows"] if row["guest"] == guest_name)
        guest = next(row for row in dashboard["guests"] if row["id"] == guest_id)
        return guest["response"], row["status"]

    def test_pending_cards_precede_history_by_created_at(self):
        extensions = [self.extension("P001", "G002", "sent", "2026-08-06T22:00:00"),
                      self.extension("P002", "G003", "confirmed", "2026-08-06T22:10:00"),
                      self.extension("P003", "G001", "sent", "2026-08-06T22:20:00")]
        pending = [self.related("P004", "invoice_cancellation", "P001", "G002", "pending", "2026-08-06T23:31:00"),
                   self.related("P005", "refund", "P002", "G003", "pending", "2026-08-06T23:32:00"),
                   self.related("P006", "payment_reminder", "P003", "G001", "pending", "2026-08-06T23:33:00")]
        staff = {"id": "P007", "type": "staff_note", "guest_id": "G004", "guest_name": "Liu Wei",
                 "note": "Staff follow-up", "status": "pending", "created_at": "2026-08-06T23:34:00"}
        self.save(extensions + pending + [staff])
        html = self.client.get("/").get_data(as_text=True)
        positions = [html.index(f'<span>{item_id}</span>') for item_id in
                     ("P004", "P005", "P006", "P007", "P001", "P002", "P003")]
        self.assertEqual(positions, sorted(positions))
        self.assertLess(positions[3], html.index("In progress &amp; done"))
        self.assertLess(html.index("In progress &amp; done"), positions[4])
        self.assertIn("OFFICIAL EVIDENCE · AT PROPOSAL", html)

    def test_related_pending_actions_annotate_extension_and_hide_cancel_record_button(self):
        self.save([
            self.extension("P001", "G002", "sent", "2026-08-06T22:00:00"),
            self.extension("P002", "G003", "confirmed", "2026-08-06T22:01:00"),
            self.extension("P003", "G001", "sent", "2026-08-06T22:02:00"),
            self.related("P004", "invoice_cancellation", "P001", "G002", "pending", "2026-08-06T23:31:00"),
            self.related("P005", "refund", "P002", "G003", "pending", "2026-08-06T23:32:00"),
            self.related("P006", "payment_reminder", "P003", "G001", "pending", "2026-08-06T23:33:00"),
        ])
        html = self.client.get("/").get_data(as_text=True)
        self.assertNotIn('action="/proposals/P001/record-sandbox-payment"', html)
        self.assertIn('action="/proposals/P003/record-sandbox-payment"', html)
        for message in ("Cancellation proposed — review it in the queue above.",
                        "Refund proposed — review it in the queue above.",
                        "Reminder proposed — review it in the queue above."):
            self.assertIn(message, html)

    def test_guest_list_and_tracker_prioritize_related_status(self):
        scenarios = [
            ("G002", "sent", "invoice_cancellation", "pending", None, "Cancellation proposed"),
            ("G002", "cancelled", "invoice_cancellation", "done", None, "Invoice cancelled"),
            ("G003", "confirmed", "refund", "pending", None, "Refund proposed"),
            ("G003", "refunded", "refund", "done", "PENDING", "Refund pending"),
            ("G003", "refunded", "refund", "done", "COMPLETED", "Refunded"),
            ("G001", "sent", "payment_reminder", "pending", None, "Reminder proposed"),
            ("G001", "sent", "payment_reminder", "done", None, "Reminder sent"),
        ]
        for guest_id, extension_status, kind, action_status, refund_status, expected in scenarios:
            with self.subTest(expected=expected):
                extension = self.extension("P001", guest_id, extension_status, "2026-08-06T22:00:00")
                action = self.related("P002", kind, "P001", guest_id, action_status,
                                      "2026-08-06T23:30:00")
                if refund_status:
                    action["refund_status"] = refund_status
                self.save([extension, action])
                self.assertEqual(self.tracker_status(guest_id), (expected, expected))

    def test_update_button_moves_to_action_group_and_timestamp_is_single(self):
        initial = self.client.get("/").get_data(as_text=True)
        header = initial.split("</header>", 1)[0]
        self.assertIn("Official notices only · as of Aug 6 · 22:00", header)
        self.assertEqual(header.count("Official notices only · as of"), 1)
        self.assertIn("Demo only: loads a fictional 23:30 notice where two cancelled flights are restored.", header)
        self.assertLess(header.index("Simulate 23:30 airline update (demo)"), header.index("Reset demo"))
        self.assertIn('class="button button-reset" type="submit">Simulate 23:30', header)
        common.write_json(common.scenario_path(), {"notice": "update"})
        updated = self.client.get("/").get_data(as_text=True).split("</header>", 1)[0]
        self.assertNotIn("Simulate 23:30 airline update", updated)
        self.assertEqual(updated.count("Official notices only · as of Aug 6 · 23:30"), 1)


if __name__ == "__main__":
    unittest.main()
