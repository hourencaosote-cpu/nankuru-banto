"""
支払いの確認：送った請求書が支払われたかを PayPal に聞き、支払われたものは「延泊確定」にする。

使い方:  python3 check_payments.py
"""

from common import approvals_path, load_env, log_decision, read_json, write_json
from paypal_tools import _get_token, get_invoice_status

PAID = ("PAID", "MARKED_AS_PAID")


def confirm_invoice_if_paid(invoice_id, env, token=None, source="check_payments"):
    """Confirm one sent extension only after checking its current PayPal status."""
    queue = read_json(approvals_path(), [])
    item = next(
        (entry for entry in queue if entry.get("type") == "extension"
         and entry.get("invoice_id") == invoice_id and entry.get("status") == "sent"),
        None,
    )
    if item is None:
        return None

    status = get_invoice_status(env, invoice_id, token=token)
    if status in PAID:
        item["status"] = "confirmed"
        write_json(approvals_path(), queue)
        log_decision(
            "paypal", "payment_confirmed",
            {"id": item["id"], "invoice_id": invoice_id,
             "amount_usd": item["amount_usd"], "via": source},
        )
    elif source == "check_payments":
        log_decision(
            "paypal", "payment_pending",
            {"id": item["id"], "invoice_id": invoice_id, "paypal_status": status},
        )
    return {"item": item, "paypal_status": status, "confirmed": status in PAID}


def check_payments(env=None):
    """送信済み請求書を確認し、支払い済みの延泊を確定する。"""
    queue = read_json(approvals_path(), [])
    sent = [item for item in queue if item.get("type") == "extension" and item.get("status") == "sent"]
    result = {
        "checked": 0,
        "newly_confirmed": [],
        "pending": [],
        "errors": [],
        "confirmed": [item for item in queue if item.get("status") == "confirmed"],
    }
    if not sent:
        return result

    env = env or load_env()
    token = _get_token(env)
    for item in sent:
        try:
            outcome = confirm_invoice_if_paid(item["invoice_id"], env, token=token)
        except (RuntimeError, SystemExit):
            result["errors"].append(item)
            log_decision(
                "paypal",
                "payment_check_failed",
                {"id": item["id"], "invoice_id": item["invoice_id"]},
            )
            continue

        if outcome is None:
            continue
        result["checked"] += 1
        if outcome["confirmed"]:
            result["newly_confirmed"].append(outcome["item"])
            result["confirmed"].append(outcome["item"])
        else:
            result["pending"].append({"item": item, "paypal_status": outcome["paypal_status"]})
    return result


def main():
    queue = read_json(approvals_path(), [])
    sent = [item for item in queue if item.get("type") == "extension" and item.get("status") == "sent"]
    if not sent:
        print("支払い待ちの請求書はありません。")
    else:
        print(f"支払い待ちの請求書 {len(sent)} 件を PayPal で確認します。\n")
        result = check_payments()
        for item in result["newly_confirmed"]:
            print(f"  ✓ {item['guest_name']} 様：支払い済み（{item['amount_usd']:.2f} ドル）→ 延泊が確定しました")
        for pending in result["pending"]:
            item = pending["item"]
            print(f"  … {item['guest_name']} 様：まだ支払われていません（PayPal の状態: {pending['paypal_status']}）")
        for item in result["errors"]:
            print(f"  ! {item['guest_name']} 様：PayPal の確認に失敗しました。この請求書は次回もう一度確認してください。")

    confirmed = [item for item in read_json(approvals_path(), []) if item.get("status") == "confirmed"]
    if confirmed:
        print("\n延泊が確定したお客様（スタッフは部屋の延長手続きをしてください）")
        for item in confirmed:
            print(f"  ・{item['guest_name']} 様　{item['nights']}泊　{item['amount_usd']:.2f} ドル　請求書 {item['invoice_id']}")


if __name__ == "__main__":
    main()
