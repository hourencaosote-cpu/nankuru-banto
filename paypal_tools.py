"""
「手」の部分：PayPal sandbox で延泊の請求書を作る。

この関数を呼べるのは approve.py（人が承認したとき）だけ。
AI（agent.py）からは呼べないようにしてある。
"""

import base64
import json
import sys
import urllib.error
import urllib.parse
import urllib.request

# 安全のため、接続先は sandbox に固定する（Live には絶対につながない）
API_BASE = "https://api-m.sandbox.paypal.com"


def _call(method, path, token=None, body=None, basic_auth=None, form=None):
    headers = {"Accept": "application/json"}
    data = None
    if basic_auth:
        headers["Authorization"] = "Basic " + base64.b64encode(basic_auth.encode()).decode()
    if token:
        headers["Authorization"] = "Bearer " + token
    if form is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        data = urllib.parse.urlencode(form).encode()
    elif body is not None:
        headers["Content-Type"] = "application/json"
        headers["Prefer"] = "return=representation"
        data = json.dumps(body).encode()

    req = urllib.request.Request(API_BASE + path, data=data, headers=headers, method=method)
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
        "items": [{
            "name": "Typhoon stay extension (1 night)",
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
