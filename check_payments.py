"""
支払いの確認：送った請求書が支払われたかを PayPal に聞き、支払われたものは「延泊確定」にする。

使い方:  python3 check_payments.py
"""

from common import APPROVALS_PATH, load_env, log_decision, read_json, write_json
from paypal_tools import _get_token, get_invoice_status

PAID = ("PAID", "MARKED_AS_PAID")


def main():
    queue = read_json(APPROVALS_PATH, [])
    sent = [item for item in queue if item.get("type") == "extension" and item.get("status") == "sent"]
    confirmed = [item for item in queue if item.get("status") == "confirmed"]
    if not sent:
        print("支払い待ちの請求書はありません。")
    else:
        env = load_env()
        token = _get_token(env)
        print(f"支払い待ちの請求書 {len(sent)} 件を PayPal で確認します。\n")
        for item in sent:
            status = get_invoice_status(env, item["invoice_id"], token=token)
            if status in PAID:
                item["status"] = "confirmed"
                confirmed.append(item)
                log_decision("paypal", "payment_confirmed",
                             {"id": item["id"], "invoice_id": item["invoice_id"], "amount_usd": item["amount_usd"]})
                print(f"  ✓ {item['guest_name']} 様：支払い済み（{item['amount_usd']:.2f} ドル）→ 延泊が確定しました")
            else:
                print(f"  … {item['guest_name']} 様：まだ支払われていません（PayPal の状態: {status}）")
        write_json(APPROVALS_PATH, queue)

    if confirmed:
        print("\n延泊が確定したお客様（スタッフは部屋の延長手続きをしてください）")
        for item in confirmed:
            print(f"  ・{item['guest_name']} 様　{item['nights']}泊　{item['amount_usd']:.2f} ドル　請求書 {item['invoice_id']}")


if __name__ == "__main__":
    main()
