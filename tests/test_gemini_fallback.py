import io
import http.client
import json
import shutil
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

import agent
import app as web_app
import common


ROOT = Path(__file__).resolve().parents[1]


def answer(*parts):
    return {"candidates": [{"content": {"role": "model", "parts": list(parts)}}]}


def call(name, **args):
    return {"functionCall": {"name": name, "args": args}}


def http_error(code, body="busy"):
    return urllib.error.HTTPError("https://example.test", code, "error", {}, io.BytesIO(body.encode()))


def raise_http_error(code, body="busy"):
    raise http_error(code, body)


class GeminiFallbackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.data = self.root / "data"
        self.data.mkdir()
        shutil.copy(ROOT / "data" / "guests.json", self.data / "guests.json")
        shutil.copy(ROOT / "data" / "flights.json", self.data / "flights.json")
        self.replay = self.data / "replay" / "gemini_run.json"
        self.approvals = self.data / "approvals.json"
        self.log = self.root / "decision_log.jsonl"
        self.patches = [
            mock.patch.object(agent, "DATA_DIR", str(self.data)),
            mock.patch.object(agent, "REPLAY_PATH", str(self.replay)),
            mock.patch("urllib.request.urlopen", side_effect=AssertionError("Network access forbidden")),
            mock.patch("builtins.print"),
        ]
        for patch in self.patches:
            patch.start()
        self.tokens = (common._approvals_override.set(str(self.approvals)),
                       common._log_override.set(str(self.log)))

    def tearDown(self):
        common._approvals_override.reset(self.tokens[0])
        common._log_override.reset(self.tokens[1])
        for patch in reversed(self.patches):
            patch.stop()
        self.temp.cleanup()

    def _entries(self):
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]

    def _recording(self):
        self.replay.parent.mkdir(exist_ok=True)
        self.replay.write_text(json.dumps([
            answer(
                call("get_social_posts"),
                call("propose_stay_extension", guest_id="G001", nights=2, reason="Official cancellation.", message_draft="We can extend your stay."),
                call("propose_stay_extension", guest_id="G002", nights=1, reason="Official cancellation.", message_draft="We can extend your stay."),
                call("propose_stay_extension", guest_id="G003", nights=3, reason="Official cancellation.", message_draft="We can extend your stay."),
                call("propose_stay_extension", guest_id="G004", nights=1, reason="Official cancellation.", message_draft="We can extend your stay."),
                call("notify_staff", guest_id="G004", note="OTA booking needs staff follow-up."),
            ),
            answer({"text": "Review complete."}),
        ]), encoding="utf-8")

    def test_repeated_503_stops_within_budget(self):
        clock = [0.0]

        def sleep(seconds):
            clock[0] += seconds

        with mock.patch("urllib.request.urlopen", side_effect=lambda *_a, **_k: raise_http_error(503)), \
             mock.patch.object(agent.time, "monotonic", side_effect=lambda: clock[0]), \
             mock.patch.object(agent.time, "sleep", side_effect=sleep):
            with self.assertRaises(agent.GeminiUnavailable) as caught:
                agent.run_ai({"GEMINI_API_KEY": "test-value", "AI_RUN_BUDGET_SECONDS": "3"}, web=True, mode="live")
        self.assertEqual(caught.exception.code, 503)
        self.assertLessEqual(clock[0], 3)

    def test_fallback_switches_only_before_first_response(self):
        urls = []
        responses = [503, answer({"text": "Done."})]

        def urlopen(request, timeout):
            urls.append(request.full_url)
            result = responses.pop(0)
            if isinstance(result, int):
                raise_http_error(result)
            response = mock.MagicMock()
            response.__enter__.return_value.read.return_value = json.dumps(result).encode()
            return response

        env = {"GEMINI_API_KEY": "test-value", "GEMINI_MODEL": "primary", "GEMINI_FALLBACK_MODELS": "backup"}
        with mock.patch("urllib.request.urlopen", side_effect=urlopen):
            self.assertEqual(agent.run_ai(env, mode="live", web=True), "live")
        self.assertEqual([url.split("/models/")[1].split(":")[0] for url in urls], ["primary", "backup"])
        self.assertIn("backup", [entry["detail"]["model"] for entry in self._entries() if entry["action"] == "gemini_model_used"])

        urls.clear()
        responses = [answer(call("list_guests")), 503, 503, 503, 503]
        with mock.patch("urllib.request.urlopen", side_effect=urlopen), mock.patch.object(agent.time, "sleep"):
            with self.assertRaises(agent.GeminiUnavailable):
                agent.run_ai(env, mode="live", web=True)
        self.assertTrue(urls)
        self.assertEqual({url.split("/models/")[1].split(":")[0] for url in urls}, {"primary"})

    def test_auto_replays_before_any_written_item_and_gate_still_rejects_ota(self):
        self._recording()
        with mock.patch.object(agent, "call_gemini", side_effect=agent.GeminiUnavailable(503, "busy")):
            self.assertEqual(agent.run_ai({}, mode="auto", web=True), "replay")
        items = json.loads(self.approvals.read_text(encoding="utf-8"))
        self.assertEqual(sum(item["type"] == "extension" for item in items), 3)
        self.assertEqual(sum(item["type"] == "staff_note" for item in items), 1)
        actions = [entry["action"] for entry in self._entries()]
        self.assertIn("rejected_proposal", actions)
        self.assertIn("replay_mode", actions)
        self.assertIn("replay_mode", web_app.ACTION_LABELS)

    def test_auto_replays_after_timeout_or_malformed_response_before_writes(self):
        for error in (TimeoutError("private timeout detail"), KeyError("private response detail")):
            with self.subTest(error_type=type(error).__name__):
                self._recording()
                with mock.patch.object(agent, "call_gemini", side_effect=error):
                    self.assertEqual(agent.run_ai({}, mode="auto", web=True), "replay")
                items = json.loads(self.approvals.read_text(encoding="utf-8"))
                self.assertEqual(sum(item["type"] == "extension" for item in items), 3)
                self.assertEqual(sum(item["type"] == "staff_note" for item in items), 1)
                entries = self._entries()
                self.assertIn("rejected_proposal", [entry["action"] for entry in entries])
                replay_reason = next(entry["detail"]["reason"] for entry in entries
                                     if entry["action"] == "replay_mode")
                self.assertEqual(replay_reason, f"Gemini unavailable ({type(error).__name__})")
                self.assertNotIn(str(error), self.log.read_text(encoding="utf-8"))
                self.approvals.unlink()
                self.log.unlink()

    def test_cli_auto_never_replays_after_proposal_is_written(self):
        self._recording()
        responses = [answer(call("propose_stay_extension", guest_id="G001", nights=2,
                                 reason="Official cancellation.", message_draft="We can extend your stay."))]

        def model(_env, _contents):
            if responses:
                return responses.pop(0)
            raise agent.GeminiUnavailable(503, "busy")

        with self.assertRaises(agent.GeminiUnavailable):
            agent.run_ai({}, call_model=model, mode="auto", web=False)
        self.assertEqual(len(json.loads(self.approvals.read_text(encoding="utf-8"))), 1)
        self.assertNotIn("replay_mode", [entry["action"] for entry in self._entries()])

    def test_web_auto_rolls_back_two_proposals_then_replays(self):
        self._recording()
        self.approvals.write_text("[]", encoding="utf-8")
        self.log.write_text(json.dumps({"action": "existing_event", "detail": {}}) + "\n", encoding="utf-8")
        responses = [answer(
            call("propose_stay_extension", guest_id="G001", nights=2,
                 reason="Official cancellation.", message_draft="We can extend your stay."),
            call("propose_stay_extension", guest_id="G002", nights=1,
                 reason="Official cancellation.", message_draft="We can extend your stay."),
        )]

        def model(_env, _contents):
            if responses:
                return responses.pop(0)
            raise TimeoutError("private timeout detail")

        self.assertEqual(agent.run_ai({}, call_model=model, mode="auto", web=True), "replay")
        items = json.loads(self.approvals.read_text(encoding="utf-8"))
        self.assertEqual(sum(item["type"] == "extension" for item in items), 3)
        self.assertEqual(sum(item["type"] == "staff_note" for item in items), 1)
        entries = self._entries()
        actions = [entry["action"] for entry in entries]
        self.assertEqual(actions[0], "existing_event")
        self.assertEqual(actions.count("proposed_extension"), 5)
        self.assertLess(actions.index("live_run_rolled_back"), actions.index("replay_mode"))
        rollback = next(entry["detail"] for entry in entries if entry["action"] == "live_run_rolled_back")
        self.assertEqual(rollback, {"reason": "TimeoutError", "removed_items": 2})
        rollback_entry = next(entry for entry in entries if entry["action"] == "live_run_rolled_back")
        self.assertEqual(web_app._log_view(rollback_entry, {}, 0, {})["label"], "Live AI run rolled back")
        self.assertNotIn("private timeout detail", self.log.read_text(encoding="utf-8"))

    def test_web_auto_with_existing_proposal_never_rolls_back_or_replays(self):
        for name, args in (
            ("propose_stay_extension", {"guest_id": "G001", "nights": 2,
                                         "reason": "Official cancellation.", "message_draft": "A safe extension is available."}),
            ("notify_staff", {"guest_id": "G004", "note": "OTA booking needs staff follow-up."}),
        ):
            with self.subTest(first_write=name):
                self._recording()
                existing = {"id": "P001", "guest_id": "G003", "type": "extension", "status": "pending"}
                self.approvals.write_text(json.dumps([existing]), encoding="utf-8")
                calls = [answer(call(name, **args))]

                def model(_env, _contents):
                    if calls:
                        return calls.pop(0)
                    raise TimeoutError("live model timed out")

                with self.assertRaises(TimeoutError):
                    agent.run_ai({}, call_model=model, mode="auto", web=True)
                items = json.loads(self.approvals.read_text(encoding="utf-8"))
                self.assertEqual(len(items), 2)
                self.assertEqual(items[0], existing)
                self.assertNotIn("replay_mode", [entry["action"] for entry in self._entries()])
                self.assertNotIn("live_run_rolled_back", [entry["action"] for entry in self._entries()])
                self.approvals.unlink()
                self.log.unlink()

    def test_record_mode_partial_failure_never_rolls_back_or_replays(self):
        self._recording()
        responses = [answer(call("propose_stay_extension", guest_id="G001", nights=2,
                                 reason="Official cancellation.", message_draft="A safe extension is available."))]

        def model(_env, _contents):
            if responses:
                return responses.pop(0)
            raise TimeoutError("timed out")

        with self.assertRaises(TimeoutError):
            agent.run_ai({}, call_model=model, record=True, web=True)
        self.assertEqual(len(json.loads(self.approvals.read_text(encoding="utf-8"))), 1)
        self.assertNotIn("live_run_rolled_back", [entry["action"] for entry in self._entries()])
        self.assertNotIn("replay_mode", [entry["action"] for entry in self._entries()])

    def test_recording_secret_error_never_rolls_back_or_replays(self):
        self._recording()
        responses = [answer(call("propose_stay_extension", guest_id="G001", nights=2,
                                 reason="Official cancellation.", message_draft="A safe extension is available."))]

        def model(_env, _contents):
            if responses:
                return responses.pop(0)
            raise agent.RecordingSecretError(None, "Recording stopped")

        with self.assertRaises(agent.RecordingSecretError):
            agent.run_ai({}, call_model=model, mode="auto", web=True)
        self.assertEqual(len(json.loads(self.approvals.read_text(encoding="utf-8"))), 1)
        self.assertNotIn("live_run_rolled_back", [entry["action"] for entry in self._entries()])
        self.assertNotIn("replay_mode", [entry["action"] for entry in self._entries()])

    def test_failed_rollback_raises_original_error_without_replay(self):
        self._recording()
        responses = [answer(call("propose_stay_extension", guest_id="G001", nights=2,
                                 reason="Official cancellation.", message_draft="A safe extension is available."))]

        def model(_env, _contents):
            if responses:
                return responses.pop(0)
            raise TimeoutError("timed out")

        original_write = agent.write_json

        def write_or_fail(path, items):
            if items == []:
                raise OSError("rollback unavailable")
            return original_write(path, items)

        with mock.patch.object(agent, "write_json", side_effect=write_or_fail):
            with self.assertRaises(TimeoutError):
                agent.run_ai({}, call_model=model, mode="auto", web=True)
        self.assertEqual(len(json.loads(self.approvals.read_text(encoding="utf-8"))), 1)
        self.assertNotIn("replay_mode", [entry["action"] for entry in self._entries()])

    def test_thinking_level_is_only_sent_when_configured(self):
        self.assertIn("GEMINI_THINKING_LEVEL", common.ENV_KEYS)
        for level in ("", "low"):
            with self.subTest(level=level):
                response = mock.MagicMock()
                response.__enter__.return_value.read.return_value = json.dumps(answer({"text": "Done."})).encode()
                with mock.patch("urllib.request.urlopen", return_value=response) as urlopen:
                    agent.call_gemini({"GEMINI_API_KEY": "test-key", "GEMINI_THINKING_LEVEL": level}, [])
                body = json.loads(urlopen.call_args.args[0].data)
                if level:
                    self.assertEqual(body["generationConfig"]["thinkingConfig"], {"thinkingLevel": level})
                else:
                    self.assertNotIn("generationConfig", body)

    def test_record_mode_timeout_never_replays(self):
        self._recording()
        with self.assertRaises(TimeoutError):
            agent.run_ai({}, call_model=mock.Mock(side_effect=TimeoutError("timed out")), record=True)
        entries = self._entries() if self.log.exists() else []
        self.assertNotIn("replay_mode", [entry["action"] for entry in entries])

    def test_call_gemini_converts_transport_and_json_failures(self):
        failures = (
            TimeoutError("timed out"), ConnectionError("connection lost"),
            http.client.RemoteDisconnected("remote closed"),
        )
        for error in failures:
            with self.subTest(error_type=type(error).__name__), \
                 mock.patch("urllib.request.urlopen", side_effect=error) as urlopen:
                with self.assertRaises(agent.GeminiUnavailable) as caught:
                    agent.call_gemini({"GEMINI_API_KEY": "test-key"}, [])
                self.assertIsNone(caught.exception.code)
                self.assertEqual(caught.exception.cause_type, type(error).__name__)
                self.assertNotIn(str(error), str(caught.exception))
                urlopen.assert_called_once()

        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b"not JSON"
        with mock.patch("urllib.request.urlopen", return_value=response):
            with self.assertRaises(agent.GeminiUnavailable) as caught:
                agent.call_gemini({"GEMINI_API_KEY": "test-key"}, [])
        self.assertIsNone(caught.exception.code)
        self.assertEqual(caught.exception.cause_type, "JSONDecodeError")

    def test_wrapped_timeout_replay_reason_keeps_original_exception_type(self):
        self._recording()
        with mock.patch("urllib.request.urlopen", side_effect=TimeoutError("private timeout detail")):
            self.assertEqual(agent.run_ai({"GEMINI_API_KEY": "test-key"}, mode="auto", web=True), "replay")
        reason = next(entry["detail"]["reason"] for entry in self._entries()
                      if entry["action"] == "replay_mode")
        self.assertEqual(reason, "Gemini unavailable (TimeoutError)")
        self.assertNotIn("private timeout detail", self.log.read_text(encoding="utf-8"))

    def test_no_http_request_when_less_than_three_seconds_remain(self):
        for remaining in (0, 2):
            with self.subTest(remaining=remaining):
                state = agent._new_run_state({"AI_RUN_BUDGET_SECONDS": "150"}, web=True)
                state["deadline"] = agent.time.monotonic() + remaining
                token = agent._run_state.set(state)
                try:
                    with mock.patch("urllib.request.urlopen") as urlopen:
                        with self.assertRaises(agent.GeminiUnavailable) as caught:
                            agent.call_gemini({"GEMINI_API_KEY": "test-key"}, [])
                    self.assertEqual(caught.exception.code, 503)
                    urlopen.assert_not_called()
                finally:
                    agent._run_state.reset(token)

    def test_direct_replay_runs_tools_and_missing_file_preserves_live_error(self):
        self._recording()
        agent.run_agent(agent.replay_model, {})
        self.assertEqual(len(json.loads(self.approvals.read_text(encoding="utf-8"))), 4)
        self.approvals.unlink()
        self.replay.unlink()
        with mock.patch.object(agent, "call_gemini", side_effect=agent.GeminiUnavailable(503, "busy")):
            with self.assertRaises(agent.GeminiUnavailable) as caught:
                agent.run_ai({}, mode="auto")
        self.assertEqual(caught.exception.code, 503)
        self.assertFalse(self.approvals.exists())

    def test_recording_removes_thought_signature_and_allows_ordinary_words(self):
        responses = [
            answer({"thoughtSignature": "random-key-secret-value", "functionCall": {
                "name": "propose_stay_extension", "args": {
                    "guest_id": "G001", "nights": 2, "reason": "Official cancellation.",
                    "message_draft": "A safe extension is available.",
                },
            }}),
            answer({"text": "The key is guest safety; this is no secret."}),
        ]
        seen_signature = []

        def model(_env, contents):
            if len(contents) > 1:
                seen_signature.append(contents[1]["parts"][0]["thoughtSignature"])
            return responses.pop(0)

        with mock.patch("builtins.print") as printed:
            agent.run_ai({"GEMINI_API_KEY": "test-gemini-key", "PAYPAL_CLIENT_SECRET": "test-paypal-secret"},
                         call_model=model, record=True)
        saved = self.replay.read_text(encoding="utf-8")
        self.assertNotIn("thoughtSignature", saved)
        self.assertIn("The key is guest safety", saved)
        self.assertEqual(seen_signature, ["random-key-secret-value"])
        self.assertEqual(len(json.loads(self.approvals.read_text(encoding="utf-8"))), 1)
        self.assertTrue(any("延泊提案 1件、スタッフ連絡 0件" in str(item) for item in printed.call_args_list))
        self.assertTrue(any("Gemini 呼び出し 2回" in str(item) for item in printed.call_args_list))

    def test_recording_rejects_exact_api_key_without_exposing_it(self):
        key = "AIza" + "A" * 35
        response = answer({"text": f"Credential: {key}"})
        with mock.patch("builtins.print") as printed:
            with self.assertRaises(agent.GeminiUnavailable) as caught:
                agent.run_ai({"GEMINI_API_KEY": key}, call_model=lambda *_args: response, record=True)
        self.assertFalse(self.replay.exists())
        self.assertNotIn(key, str(caught.exception))
        self.assertNotIn(key, str(printed.call_args_list))

    def test_recording_rejects_paypal_secret_and_key_shaped_text(self):
        cases = [
            ({"PAYPAL_CLIENT_SECRET": "paypal-test-secret-value"}, "paypal-test-secret-value"),
            ({}, "AIza" + "B" * 35),
        ]
        for env, secret in cases:
            with self.subTest(secret_type="paypal" if env else "google-shaped"), \
                 mock.patch("builtins.print") as printed:
                with self.assertRaises(agent.GeminiUnavailable) as caught:
                    agent.run_ai(env, call_model=lambda *_args: answer({"text": f"Credential: {secret}"}),
                                 record=True)
                self.assertFalse(self.replay.exists())
                self.assertNotIn(secret, str(caught.exception))
                self.assertNotIn(secret, str(printed.call_args_list))

    def test_recording_rejects_zero_new_proposals(self):
        self.approvals.write_text(json.dumps([{"type": "extension", "status": "pending"}]), encoding="utf-8")
        with mock.patch("builtins.print") as printed:
            with self.assertRaises(agent.GeminiUnavailable):
                agent.run_ai({}, call_model=lambda *_args: answer({"text": "Done."}), record=True)
        self.assertFalse(self.replay.exists())
        self.assertEqual(len(json.loads(self.approvals.read_text(encoding="utf-8"))), 1)
        self.assertTrue(any("延泊提案が作成されませんでした" in str(item) for item in printed.call_args_list))

    def test_cli_has_no_web_deadline(self):
        clock = [0.0]
        responses = [answer(call("list_guests")), answer({"text": "Done."})]

        def urlopen(_request, timeout):
            result = responses.pop(0)
            if len(responses) == 1:
                clock[0] = 200.0
            opened = mock.MagicMock()
            opened.__enter__.return_value.read.return_value = json.dumps(result).encode()
            return opened

        env = {"GEMINI_API_KEY": "test-key", "AI_RUN_BUDGET_SECONDS": "150"}
        with mock.patch.object(agent.time, "monotonic", side_effect=lambda: clock[0]), \
             mock.patch("urllib.request.urlopen", side_effect=urlopen):
            self.assertEqual(agent.run_ai(env, mode="live", web=False), "live")
        clock[0] = 0.0
        responses = [answer(call("list_guests")), answer({"text": "Done."})]
        with mock.patch.object(agent.time, "monotonic", side_effect=lambda: clock[0]), \
             mock.patch("urllib.request.urlopen", side_effect=urlopen):
            with self.assertRaises(agent.GeminiUnavailable) as caught:
                agent.run_ai(env, mode="live", web=True)
        self.assertEqual(caught.exception.code, 503)

    def test_429_quota_does_not_retry_and_log_is_redacted(self):
        body = "quota RESOURCE_EXHAUSTED PerDay key=AIzaSUPERPRIVATE0123456789"
        with mock.patch("urllib.request.urlopen", side_effect=lambda *_a, **_k: raise_http_error(429, body)) as urlopen, \
             self.assertLogs(level="WARNING") as logs:
            with self.assertRaises(agent.GeminiUnavailable) as caught:
                agent.run_ai({"GEMINI_API_KEY": "test-value"}, mode="live", web=True)
        self.assertEqual(caught.exception.code, 429)
        urlopen.assert_called_once()
        self.assertNotIn("AIza", " ".join(logs.output))


if __name__ == "__main__":
    unittest.main()
