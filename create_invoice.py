"""
PayPal sandbox で「台風による延泊」の請求書を1枚作るテストスクリプト。

- 追加インストール不要（Python 3 の標準機能だけで動く）
- 接続先は sandbox（練習用）に固定。本物のお金は動かない
- キーは同じフォルダの env.txt ファイルから読む（チャットには貼らない）

使い方:
  1. このファイルと同じフォルダに env.txt を作る（env.example を参照）
  2. ターミナルで:  python3 create_invoice.py
"""

import base64
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

# 安全のため、接続先は sandbox に固定する（Live には絶対につながない）
API_BASE = "https://api-m.sandbox.paypal.com"


def load_env():
    """同じフォルダの .env（なければ env.txt）から KEY=VALUE を読み込む。"""
    here = os.path.dirname(os.path.abspath(__file__))
    full = None
    for name in (".env", "env.txt"):
        candidate = os.path.join(here, name)
        if os.path.exists(candidate):
            full = candidate
            break
    if full is None:
        sys.exit(f"設定ファイルが見つかりません（{here} に env.txt を置いてください）。\n"
                 "env.example をコピーして env.txt という名前にし、キーを入れてください。")
    env = {}
    with open(full, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def call(method, path, token=None, body=None, basic_auth=None, form=None):
    """PayPal API を呼び出し、(ステータス, JSON) を返す。"""
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
        reason = str(e.reason)
        hint = "インターネットにつながっているか確認してください。"
        if "CERTIFICATE_VERIFY_FAILED" in reason:
            hint = ("Mac の Python の証明書設定が必要です。Finder で「アプリケーション > Python 3.x」を開き、"
                    "「Install Certificates.command」をダブルクリックしてから、もう一度実行してください。")
        sys.exit(f"PayPal に接続できませんでした: {reason}\n{hint}")


def main():
    env = load_env()
    client_id = env.get("PAYPAL_CLIENT_ID", "")
    secret = env.get("PAYPAL_CLIENT_SECRET", "")
    buyer_email = env.get("BUYER_EMAIL", "")  # 空なら下書きの作成だけで止める
    currency = env.get("CURRENCY", "USD")

    if not client_id or not secret:
        sys.exit(".env に PAYPAL_CLIENT_ID と PAYPAL_CLIENT_SECRET を入れてください。")

    # 1. ログイン（アクセストークンの取得）
    print("1/3 PayPal sandbox にログイン中...")
    status, res = call("POST", "/v1/oauth2/token",
                       basic_auth=f"{client_id}:{secret}",
                       form={"grant_type": "client_credentials"})
    if status != 200:
        print("ログインに失敗しました。Client ID と Secret を確認してください。")
        print(json.dumps(res, ensure_ascii=False, indent=2))
        sys.exit(1)
    token = res["access_token"]
    print("    OK")

    # 2. 請求書の下書きを作る（台風で延泊2泊、20%の台風割引）
    print("2/3 請求書の下書きを作成中...")
    invoice = {
        "detail": {
            "currency_code": currency,
            "note": "Your flight was cancelled due to the typhoon. "
                    "We have extended your stay with a 20% typhoon discount.",
        },
        "items": [
            {
                "name": "Typhoon stay extension (1 night)",
                "quantity": "2",
                "unit_amount": {"currency_code": currency, "value": "80.00"},
                "discount": {"percent": "20"},
            }
        ],
    }
    if buyer_email:
        invoice["primary_recipients"] = [{"billing_info": {"email_address": buyer_email}}]

    status, res = call("POST", "/v2/invoicing/invoices", token=token, body=invoice)
    if status not in (200, 201):
        print("請求書の作成に失敗しました。以下のエラー内容を相談してください。")
        print(json.dumps(res, ensure_ascii=False, indent=2))
        sys.exit(1)

    invoice_id = res.get("id")
    amount = res.get("amount", {}).get("value")
    print(f"    OK  請求書ID: {invoice_id}  合計: {amount} {currency}  状態: {res.get('status')}")

    if not buyer_email:
        print("\nBUYER_EMAIL が空なので、下書きの作成までで終了しました。")
        print("送信まで試すときは、env.txt の BUYER_EMAIL に sandbox の個人アカウントのメールアドレスを入れてください。")
        return

    # 3. 請求書を送る（sandbox のテスト用の客に届く）
    print("3/3 請求書を送信中...")
    status, res = call("POST", f"/v2/invoicing/invoices/{invoice_id}/send",
                       token=token, body={"send_to_invoicer": True})
    if status not in (200, 202):
        print("送信に失敗しました。以下のエラー内容を相談してください。")
        print(json.dumps(res, ensure_ascii=False, indent=2))
        sys.exit(1)

    status, detail = call("GET", f"/v2/invoicing/invoices/{invoice_id}", token=token)
    print(f"    OK  状態: {detail.get('status')}")
    view_url = detail.get("detail", {}).get("metadata", {}).get("recipient_view_url")
    if view_url:
        print(f"\n客が見る請求書のページ: {view_url}")


if __name__ == "__main__":
    main()
