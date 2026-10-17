import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app as web_app
import common
import paypal_tools


class RenderFoundationTests(unittest.TestCase):
    def test_environment_variables_work_without_a_settings_file(self):
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(common, "HERE", directory), \
             mock.patch.dict(os.environ, {"GEMINI_API_KEY": "test-gemini-key"}, clear=True):
            self.assertEqual(common.load_env(), {"GEMINI_API_KEY": "test-gemini-key"})

    def test_env_txt_takes_priority_and_environment_fills_missing_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "env.txt").write_text("GEMINI_API_KEY=file-key\n", encoding="utf-8")
            with mock.patch.object(common, "HERE", directory), \
                 mock.patch.dict(os.environ, {
                     "GEMINI_API_KEY": "environment-key",
                     "PAYPAL_CLIENT_ID": "environment-client",
                 }, clear=True):
                self.assertEqual(common.load_env(), {
                    "GEMINI_API_KEY": "file-key",
                    "PAYPAL_CLIENT_ID": "environment-client",
                })

    def test_dotenv_is_used_when_env_txt_is_absent(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, ".env").write_text("GEMINI_MODEL=file-model\n", encoding="utf-8")
            with mock.patch.object(common, "HERE", directory), \
                 mock.patch.dict(os.environ, {"GEMINI_MODEL": "environment-model"}, clear=True):
                self.assertEqual(common.load_env(), {"GEMINI_MODEL": "file-model"})

    def test_dashboard_opens_with_no_settings(self):
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(common, "HERE", directory), \
             mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(common.load_env(), {})
            self.assertEqual(web_app.app.test_client().get("/").status_code, 200)

    def test_healthz_only_returns_status(self):
        with mock.patch.dict(os.environ, {"SECRET_KEY": "test-secret-do-not-expose"}, clear=True):
            response = web_app.app.test_client().get("/healthz")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"status": "ok"})
        self.assertNotIn(b"test-secret-do-not-expose", response.data)

    def test_render_uses_configured_secret_and_secure_session_cookie(self):
        spec = importlib.util.spec_from_file_location("render_config_test_app", Path(web_app.__file__))
        module = importlib.util.module_from_spec(spec)
        with mock.patch.dict(os.environ, {"SECRET_KEY": "configured-test-secret", "RENDER": "true"}, clear=True):
            spec.loader.exec_module(module)

        self.assertEqual(module.app.config["SECRET_KEY"], "configured-test-secret")
        self.assertTrue(module.app.config["SESSION_COOKIE_SECURE"])

    def test_invoice_contains_business_name_without_calling_paypal(self):
        with mock.patch.dict(os.environ, {"INVOICE_BUSINESS_NAME": "Test Demo Inn"}, clear=True), \
             mock.patch.object(paypal_tools, "_get_token", return_value="test-token"), \
             mock.patch.object(paypal_tools, "_call", return_value=(201, {"id": "INV-TEST"})) as call:
            paypal_tools.create_extension_invoice({}, 2, 80, 20, "Demo note")

        self.assertEqual(call.call_args.kwargs["body"]["invoicer"], {"business_name": "Test Demo Inn"})

    def test_invoice_business_name_has_demo_default(self):
        with mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch.object(paypal_tools, "_get_token", return_value="test-token"), \
             mock.patch.object(paypal_tools, "_call", return_value=(201, {"id": "INV-TEST"})) as call:
            paypal_tools.create_extension_invoice({}, 1, 80, 0, "Demo note")

        self.assertEqual(call.call_args.kwargs["body"]["invoicer"], {"business_name": "Nankuru Demo Inn"})

    def test_record_invoice_payment_uses_sandbox_v2_external_payment(self):
        with mock.patch.object(paypal_tools, "_get_token", return_value="test-token"), \
             mock.patch.object(paypal_tools, "_call", return_value=(200, {"payment_id": "EXTR-TEST"})) as call:
            paypal_tools.record_invoice_payment({}, "INV-TEST", 128.0)

        self.assertEqual(paypal_tools.API_BASE, "https://api-m.sandbox.paypal.com")
        self.assertEqual(call.call_args.args, ("POST", "/v2/invoicing/invoices/INV-TEST/payments"))
        self.assertEqual(call.call_args.kwargs, {
            "token": "test-token",
            "body": {
                "method": "OTHER",
                "amount": {"currency_code": "USD", "value": "128.00"},
                "note": "Recorded in sandbox demo by the manager",
            },
        })


if __name__ == "__main__":
    unittest.main()
