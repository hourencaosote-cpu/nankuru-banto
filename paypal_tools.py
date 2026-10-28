"""
「手」の部分：PayPal sandbox で延泊の請求書を作る。

AI は請求書の状態だけを読み、請求・催促・支払い記録は人の承認後だけ呼ぶ。
"""

import base64
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal, InvalidOperation

# 安全のため、接続先は sandbox に固定する（Live には絶対につながない）
API_BASE = "https://api-m.sandbox.paypal.com"


def _call(method, path, token=None, body=None, basic_auth=None, form=None, headers=None):
    request_headers = {"Accept": "application/json"}
    data = None
    if basic_auth:
        request_headers["Authorization"] = "Basic " + base64.b64encode(basic_auth.encode()).decode()
    if token:
        request_headers["Authorization"] = "Bearer " + token
    if form is not None:
        request_headers["Content-Type"] = "application/x-www-form-urlencoded"
        data = urllib.parse.urlencode(form).encode()
    elif body is not None:
        request_headers["Content-Type"] = "application/json"
        request_headers["Prefer"] = "return=representation"
        data = json.dumps(body).encode()
    if headers:
        request_headers.update(headers)

    req = urllib.request.Request(API_BASE + path, data=data, headers=request_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            text = res.read().decode()
            return res.status, (json.loads(text) if text else {})
    except urllib.error.HTTPError as e:
        text = e.read().decode()
        try:
            return e.code, json.loads(text)
        except json.JSONDecodeError:
            return e.code, {"raw": text}
    except urllib.error.URLError as e:
        sys.exit(f"PayPal に接続できませんでした: {e.reason}")


def _get_token(env):
    client_id = env.get("PAYPAL_CLIENT_ID", "")
    secret = env.get("PAYPAL_CLIENT_SECRET", "")
    if not client_id or not secret:
        raise RuntimeError("env.txt に PAYPAL_CLIENT_ID と PAYPAL_CLIENT_SECRET を入れてください。")
    status, res = _call("POST", "/v1/oauth2/token",
                        basic_auth=f"{client_id}:{secret}",
                        form={"grant_type": "client_credentials"})
    if status != 200:
        raise RuntimeError(f"PayPal へのログインに失敗しました: {res}")
    return res["access_token"]


def create_extension_invoice(env, nights, nightly_rate_usd, discount_percent, note, recipient_email=""):
    """延泊の請求書を「下書き」で作る。戻り値は (請求書ID, 合計金額)。
    recipient_email があれば宛先を入れておく（送信は send_invoice で別に行う）。"""
    token = _get_token(env)
    currency = "USD"
    invoice = {
        "detail": {"currency_code": currency, "note": note[:3900]},
        "invoicer": {"business_name": os.environ.get("INVOICE_BUSINESS_NAME") or "Nankuru Demo Inn"},
        "items": [{
            "name": "Typhoon stay extension (per night)",
            "quantity": str(nights),
            "unit_amount": {"currency_code": currency, "value": f"{nightly_rate_usd:.2f}"},
            "discount": {"percent": str(discount_percent)},
        }],
    }
    if recipient_email:
        invoice["primary_recipients"] = [{"billing_info": {"email_address": recipient_email}}]
    status, res = _call("POST", "/v2/invoicing/invoices", token=token, body=invoice)
    if status not in (200, 201):
        raise RuntimeError(f"請求書の作成に失敗しました: {json.dumps(res, ensure_ascii=False)}")
    return res.get("id"), res.get("amount", {}).get("value")


def send_invoice(env, invoice_id):
    """下書きの請求書を客に送る。戻り値は (状態, 客が支払うページのURL)。"""
    token = _get_token(env)
    status, res = _call("POST", f"/v2/invoicing/invoices/{invoice_id}/send",
                        token=token, body={"send_to_invoicer": True})
    if status not in (200, 202):
        raise RuntimeError(f"請求書の送信に失敗しました: {json.dumps(res, ensure_ascii=False)}")
    status, detail = _call("GET", f"/v2/invoicing/invoices/{invoice_id}", token=token)
    view_url = detail.get("detail", {}).get("metadata", {}).get("recipient_view_url", "")
    return detail.get("status", ""), view_url


def get_invoice_status(env, invoice_id, token=None):
    """請求書の今の状態（SENT / PAID など）を PayPal に聞く。"""
    token = token or _get_token(env)
    status, res = _call("GET", f"/v2/invoicing/invoices/{invoice_id}", token=token)
    if status != 200:
        raise RuntimeError(f"請求書の確認に失敗しました: {json.dumps(res, ensure_ascii=False)}")
    return res.get("status", "")


def read_invoice(env, invoice_id):
    """Return only the invoice fields needed by the read-only agent tool."""
    token = _get_token(env)
    path = f"/v2/invoicing/invoices/{urllib.parse.quote(str(invoice_id), safe='')}"
    status, invoice = _call("GET", path, token=token)
    if status != 200:
        raise RuntimeError("PayPal could not read the invoice.")
    detail = invoice.get("detail", {})
    metadata = detail.get("metadata", {})
    due_amount = invoice.get("due_amount") or {}
    return {
        "status": invoice.get("status"),
        "due_amount": due_amount.get("value") if isinstance(due_amount, dict) else None,
        "viewed_by_recipient": bool(detail.get("viewed_by_recipient", False)),
        "last_sent_time": metadata.get("last_sent_time"),
    }


def _usd_amount(amount):
    try:
        value = Decimal(str(amount))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError("The saved refund amount is invalid.") from None
    if not value.is_finite() or value <= 0 or value != value.quantize(Decimal("0.01")):
        raise ValueError("The saved refund amount is invalid.")
    return f"{value:.2f}"


def get_invoice_payment(env, invoice_id):
    """Return only the first invoice payment transaction needed for a refund."""
    token = _get_token(env)
    path = f"/v2/invoicing/invoices/{urllib.parse.quote(str(invoice_id), safe='')}"
    status, invoice = _call("GET", path, token=token)
    if status != 200:
        raise RuntimeError("PayPal could not read the invoice payment.")
    payments = invoice.get("payments") or {}
    transactions = payments.get("transactions") if isinstance(payments, dict) else None
    if not isinstance(transactions, list) or not transactions:
        return None
    transaction = transactions[0]
    if not isinstance(transaction, dict):
        return None
    amount = transaction.get("amount") or {}
    return {
        "payment_id": transaction.get("payment_id"),
        "type": transaction.get("type"),
        "amount": {"currency_code": amount.get("currency_code"), "value": amount.get("value")}
        if isinstance(amount, dict) else None,
    }


def get_capture(env, capture_id):
    """Read a PayPal capture without exposing payer or merchant details."""
    token = _get_token(env)
    path = f"/v2/payments/captures/{urllib.parse.quote(str(capture_id), safe='')}"
    status, capture = _call("GET", path, token=token)
    if status != 200:
        raise RuntimeError("PayPal could not read the captured payment.")
    amount = capture.get("amount") or {}
    return {"status": capture.get("status"),
            "amount": {"currency_code": amount.get("currency_code"), "value": amount.get("value")}
            if isinstance(amount, dict) else None}


def refund_capture(env, capture_id, amount, request_id, note):
    """Issue one idempotent Sandbox refund for a captured PayPal payment."""
    if not request_id:
        raise ValueError("A saved refund request ID is required.")
    token = _get_token(env)
    path = f"/v2/payments/captures/{urllib.parse.quote(str(capture_id), safe='')}/refund"
    status, refund = _call("POST", path, token=token, body={
        "amount": {"currency_code": "USD", "value": _usd_amount(amount)},
        "note_to_payer": str(note)[:255],
    }, headers={"PayPal-Request-Id": request_id})
    if status not in (200, 201):
        raise RuntimeError("PayPal could not refund the captured payment.")
    return {"refund_id": refund.get("id"), "status": refund.get("status")}


def record_external_refund(env, invoice_id, amount):
    """Record a refund of a manager-entered external Sandbox payment."""
    token = _get_token(env)
    path = f"/v2/invoicing/invoices/{urllib.parse.quote(str(invoice_id), safe='')}/refunds"
    status, refund = _call("POST", path, token=token, body={
        "method": "OTHER",
        "amount": {"currency_code": "USD", "value": _usd_amount(amount)},
    })
    if status != 200:
        raise RuntimeError("PayPal could not record the external refund.")
    return {"refund_id": refund.get("refund_id")}


def send_reminder(env, invoice_id, subject, note):
    """Send a reminder only from the manager approval path."""
    try:
        token = _get_token(env)
        path = f"/v2/invoicing/invoices/{urllib.parse.quote(str(invoice_id), safe='')}/remind"
        status, _ = _call("POST", path, token=token, body={
            "subject": subject,
            "note": note,
            "send_to_invoicer": True,
        })
    except SystemExit:
        raise RuntimeError("PayPal could not send the invoice reminder.") from None
    if status != 204:
        raise RuntimeError("PayPal could not send the invoice reminder.")


def cancel_invoice(env, invoice_id, note):
    """Cancel a sent sandbox invoice and let PayPal notify both parties."""
    token = _get_token(env)
    path = f"/v2/invoicing/invoices/{urllib.parse.quote(str(invoice_id), safe='')}/cancel"
    status, _ = _call("POST", path, token=token, body={
        "subject": "Your stay extension invoice was cancelled",
        "note": note,
        "send_to_recipient": True,
        "send_to_invoicer": True,
    })
    if status != 204:
        raise RuntimeError("PayPal could not cancel the invoice.")


def delete_invoice_draft(env, invoice_id):
    """Delete an invoice draft that has never been sent."""
    token = _get_token(env)
    path = f"/v2/invoicing/invoices/{urllib.parse.quote(str(invoice_id), safe='')}"
    status, _ = _call("DELETE", path, token=token)
    if status != 204:
        raise RuntimeError("PayPal could not delete the invoice draft.")


def verify_webhook_signature(env, headers, event):
    """Ask PayPal Sandbox to verify a received webhook before using it."""
    header_fields = {
        "auth_algo": "PAYPAL-AUTH-ALGO",
        "cert_url": "PAYPAL-CERT-URL",
        "transmission_id": "PAYPAL-TRANSMISSION-ID",
        "transmission_sig": "PAYPAL-TRANSMISSION-SIG",
        "transmission_time": "PAYPAL-TRANSMISSION-TIME",
    }
    header_values = {name.lower(): value for name, value in headers.items()}
    values = {field: header_values.get(header.lower()) for field, header in header_fields.items()}
    webhook_id = env.get("PAYPAL_WEBHOOK_ID")
    if not webhook_id or not all(values.values()):
        return False

    status, response = _call(
        "POST", "/v1/notifications/verify-webhook-signature",
        token=_get_token(env),
        body={**values, "webhook_id": webhook_id, "webhook_event": event},
    )
    if status != 200:
        raise RuntimeError("PayPal webhook verification is unavailable.")
    return response.get("verification_status") == "SUCCESS"


def record_invoice_payment(env, invoice_id, amount):
    """Record a manager-entered external payment on a sandbox invoice."""
    if not invoice_id:
        raise ValueError("A sent invoice ID is required.")
    try:
        total = Decimal(str(amount))
    except (InvalidOperation, ValueError):
        raise ValueError("The saved invoice amount is invalid.") from None
    if not total.is_finite() or total <= 0:
        raise ValueError("The saved invoice amount is invalid.")

    token = _get_token(env)
    path = f"/v2/invoicing/invoices/{urllib.parse.quote(str(invoice_id), safe='')}/payments"
    status, _response = _call(
        "POST", path, token=token,
        body={
            "method": "OTHER",
            "amount": {"currency_code": "USD", "value": f"{total:.2f}"},
            "note": "Recorded in sandbox demo by the manager",
        },
    )
    if status not in (200, 201, 204):
        raise RuntimeError("PayPal could not record the sandbox payment.")
