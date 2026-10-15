"""
「安全装置」の最後の関門：責任者（人）が承認待ちリストを確認し、承認した提案だけを実行する。

- 延泊の提案：承認すると、PayPal sandbox に請求書が作られ、客（テスト用の買い手）に送られる。
  客が請求書を支払えば延泊が確定する。env.txt の BUYER_EMAIL が空なら下書きを作るだけ。
- スタッフへの連絡：内容を確認して「確認済み」にするだけ。お金は動かない。

使い方:  python3 approve.py
"""

from common import APPROVALS_PATH, load_env, log_decision, read_json, write_json
from paypal_tools import create_extension_invoice, send_invoice


def ask(prompt):
    while True:
        answer = input(prompt).strip().lower()
        if answer in ("y", "n", "s"):
            return answer
        print("  y（承認）、n（却下）、s（あとで）のどれかを入力してください。")


def final_message(item):
    """AIの下書き＋システムが書く金額と支払い案内（古い提案で price_line がない場合は下書きだけ）。"""
    price = item.get("price_line", "")
    return item["message_draft"] + (f"\n\n{price}" if price else "")


def main():
    queue = read_json(APPROVALS_PATH, [])
    pending = [item for item in queue if item["status"] == "pending"]
    if not pending:
        print("承認待ちの提案はありません。先に  python3 agent.py  を実行してください。")
        return

    env = load_env()
    # デモでは、全員分の請求書を sandbox のテスト用の買い手1人に送る（本物の客には送らない）
    buyer_email = env.get("BUYER_EMAIL", "")
    if not buyer_email:
        print("（env.txt の BUYER_EMAIL が空なので、請求書は下書きを作るだけにします）\n")
    print(f"承認待ちが {len(pending)} 件あります。\n")

    for item in pending:
        print("=" * 60)
        if item["type"] == "extension":
            print(f"[{item['id']}] 延泊の提案　{item['guest_name']} 様（{item['language']}）")
            print(f"  内容　: {item['nights']}泊 × {item['nightly_rate_usd']}ドル、台風割引 {item['discount_percent']}%")
            print(f"  合計　: {item['amount_usd']:.2f} ドル（金額はシステムが計算）")
            print(f"  根拠　: {item['evidence']}")
            print(f"  AIの理由: {item['reason']}")
            message = final_message(item)
            print("  客に届く案内文（最後の段落はシステムが金額を書き足したもの）:\n    " + message.replace("\n", "\n    "))
            action = "請求書を作って客に送りますか" if buyer_email else "請求書（下書き）を作りますか"
            answer = ask(f"\nこの延泊を承認して、PayPal で{action}？ y / n / s : ")
            if answer == "y":
                try:
                    invoice_id, amount = create_extension_invoice(
                        env, item["nights"], item["nightly_rate_usd"], item["discount_percent"],
                        note=message, recipient_email=buyer_email)
                    item["invoice_id"] = invoice_id
                    item["status"] = "approved"
                    if buyer_email:
                        invoice_status, pay_url = send_invoice(env, invoice_id)
                        item["status"] = "sent"
                        item["pay_url"] = pay_url
                except RuntimeError as e:
                    print(f"  PayPal でエラーが起きました。\n  {e}")
                    if "invoice_id" not in item:
                        print("  この件は承認待ちのまま残します。")
                        continue
                log_decision("human", "approved_extension",
                             {"id": item["id"], "invoice_id": item["invoice_id"], "status": item["status"]})
                if item["status"] == "sent":
                    print(f"  → 承認しました。請求書を客に送りました（ID: {item['invoice_id']}、合計 {amount} ドル）。")
                    print(f"    客が支払うページ: {item.get('pay_url') or '（PayPal の画面で確認してください）'}")
                else:
                    print(f"  → 承認しました。PayPal に請求書の下書きを作りました（ID: {item['invoice_id']}、合計 {amount} ドル）。")
            elif answer == "n":
                item["status"] = "rejected"
                log_decision("human", "rejected_extension", {"id": item["id"]})
                print("  → 却下しました。何も実行していません。")
            else:
                print("  → あとで確認します。")
        else:
            print(f"[{item['id']}] スタッフへの連絡　{item['guest_name']} 様")
            print(f"  内容: {item['note']}")
            answer = ask("\n確認しましたか？ y（確認済みにする）/ n（不要として閉じる）/ s（あとで）: ")
            if answer in ("y", "n"):
                item["status"] = "acknowledged" if answer == "y" else "dismissed"
                log_decision("human", item["status"], {"id": item["id"]})
        write_json(APPROVALS_PATH, queue)  # 1件ずつ保存（途中でやめても結果が残る）
        print()

    print("すべての確認が終わりました。判断の記録は logs/decision_log.jsonl に残っています。")


if __name__ == "__main__":
    main()
