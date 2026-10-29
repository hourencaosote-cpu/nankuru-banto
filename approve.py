"""
「安全装置」の最後の関門：責任者（人）が承認待ちリストを確認し、承認した提案だけを実行する。

- 延泊の提案：承認すると、PayPal sandbox に請求書が作られ、客（テスト用の買い手）に送られる。
  客が請求書を支払えば延泊が確定する。env.txt の BUYER_EMAIL が空なら下書きを作るだけ。
- スタッフへの連絡：内容を確認して「確認済み」にするだけ。お金は動かない。

使い方:  python3 approve.py
"""

import os
from decimal import Decimal, InvalidOperation

import common
from common import approvals_path, flights_path, load_env, log_decision, read_json, write_json
from check_payments import PAID, confirm_invoice_if_paid
from paypal_tools import (cancel_invoice, create_extension_invoice, delete_invoice_draft,
                          get_capture, get_invoice_payment, get_invoice_status, read_invoice,
                          record_external_refund, refund_capture, send_invoice, send_reminder)


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


def _find_item(queue, item_id):
    for item in queue:
        if item.get("id") == item_id:
            return item
    raise ValueError(f"Approval item '{item_id}' was not found.")


def approve_extension(item_id, env=None, before_create=None, on_invoice_created=None,
                      on_invoice_ready=None):
    """人が承認した延泊だけを PayPal sandbox の請求書にする。"""
    queue = read_json(approvals_path(), [])
    item = _find_item(queue, item_id)
    if item.get("type") != "extension":
        raise ValueError("Only stay-extension proposals can be approved here.")
    if item.get("status") not in ("pending", "approved"):
        raise ValueError("This proposal is no longer waiting for approval.")

    guest_id = item.get("guest_id")
    guest = next((entry for entry in read_json(os.path.join(common.DATA_DIR, "guests.json"), {}).get("guests", [])
                  if entry.get("id") == guest_id), None)
    flight_number = guest.get("departure_flight") if guest else None
    flight_status = read_json(flights_path(), {}).get("flights", {}).get(flight_number, {}).get("status")
    if flight_status != "cancelled":
        log_decision("gate", "approval_blocked_notice_changed", {
            "id": item_id, "guest_id": guest_id, "flight": flight_number, "status": flight_status,
        })
        raise ValueError("The official notice no longer shows this guest’s flight as cancelled. No invoice was created.")

    env = env or load_env()
    buyer_email = env.get("BUYER_EMAIL", "")
    amount = item.get("amount_usd")
    resumed_draft = item.get("status") == "approved"

    if not resumed_draft:
        message = final_message(item)
        if before_create:
            before_create()
        try:
            invoice_id, amount = create_extension_invoice(
                env,
                item["nights"],
                item["nightly_rate_usd"],
                item["discount_percent"],
                note=message,
                recipient_email=buyer_email,
            )
        except (RuntimeError, SystemExit):
            log_decision("human", "approval_failed", {"id": item_id, "stage": "create_invoice"})
            raise
        if on_invoice_created:
            on_invoice_created()
        item["invoice_id"] = invoice_id
        item["status"] = "approved"
        write_json(approvals_path(), queue)

    if on_invoice_ready:
        on_invoice_ready(item["invoice_id"])

    if buyer_email:
        try:
            invoice_status, pay_url = send_invoice(env, item["invoice_id"])
        except (RuntimeError, SystemExit):
            log_decision(
                "human",
                "approved_extension",
                {"id": item_id, "invoice_id": item["invoice_id"], "status": "approved", "send_failed": True},
            )
            raise
        item["status"] = "sent"
        item["paypal_status"] = invoice_status
        item["pay_url"] = pay_url

    write_json(approvals_path(), queue)
    log_decision(
        "human",
        "sent_extension_invoice" if resumed_draft and buyer_email else "approved_extension",
        {"id": item_id, "invoice_id": item["invoice_id"], "status": item["status"]},
    )
    return item, amount


def reject_extension(item_id):
    queue = read_json(approvals_path(), [])
    item = _find_item(queue, item_id)
    if item.get("type") != "extension" or item.get("status") != "pending":
        raise ValueError("This proposal is no longer waiting for a decision.")
    item["status"] = "rejected"
    write_json(approvals_path(), queue)
    log_decision("human", "rejected_extension", {"id": item_id})
    return item


def approve_cancellation(item_id, env=None):
    """A manager cancels only an unpaid invoice after a fresh PayPal lookup."""
    queue = read_json(approvals_path(), [])
    cancellation = _find_item(queue, item_id)
    if cancellation.get("type") != "invoice_cancellation" or cancellation.get("status") != "pending":
        raise ValueError("This cancellation is no longer waiting for approval.")
    extension = _find_item(queue, cancellation.get("proposal_id"))
    if extension.get("type") == "extension" and extension.get("status") == "cancelled":
        cancellation["status"] = "done"
        write_json(approvals_path(), queue)
        log_decision("human", "approved_cancellation", {
            "id": item_id, "proposal_id": extension["id"], "guest_id": extension.get("guest_id"),
        })
        return {"status": "done", "item": cancellation}
    if extension.get("type") != "extension" or extension.get("status") not in {"approved", "sent"}:
        raise ValueError("The original extension is no longer eligible for cancellation.")
    invoice_id = extension.get("invoice_id")
    if not invoice_id:
        raise ValueError("The original extension has no saved PayPal invoice.")

    env = env or load_env()
    try:
        status = get_invoice_status(env, invoice_id)
        if status in PAID:
            outcome = confirm_invoice_if_paid(invoice_id, env, source="cancellation", allow_draft=True)
            if outcome is None or not outcome["confirmed"]:
                raise RuntimeError("PayPal payment status changed during confirmation.")
            # The confirmation function writes the extension; reload before saving the proposal.
            queue = read_json(approvals_path(), [])
            cancellation = _find_item(queue, item_id)
            cancellation["status"] = "superseded"
            write_json(approvals_path(), queue)
            log_decision("human", "cancellation_skipped_paid", {
                "id": item_id, "proposal_id": extension["id"], "guest_id": extension.get("guest_id"),
            })
            return {"status": "superseded", "item": cancellation}
        if extension["status"] == "sent":
            if status not in {"SENT", "UNPAID"}:
                raise RuntimeError("PayPal invoice is not in a cancellable sent state.")
            cancel_invoice(env, invoice_id, cancellation["message_draft"])
        else:
            if status != "DRAFT":
                raise RuntimeError("PayPal invoice is not a draft.")
            delete_invoice_draft(env, invoice_id)
    except (Exception, SystemExit) as error:
        log_decision("human", "cancellation_failed", {
            "id": item_id, "proposal_id": extension["id"],
            "guest_id": extension.get("guest_id"), "error_type": type(error).__name__,
        })
        raise

    extension["status"] = "cancelled"
    cancellation["status"] = "done"
    write_json(approvals_path(), queue)
    log_decision("human", "approved_cancellation", {
        "id": item_id, "proposal_id": extension["id"], "guest_id": extension.get("guest_id"),
    })
    return {"status": "done", "item": cancellation}


def reject_cancellation(item_id):
    queue = read_json(approvals_path(), [])
    item = _find_item(queue, item_id)
    if item.get("type") != "invoice_cancellation" or item.get("status") != "pending":
        raise ValueError("This cancellation is no longer waiting for a decision.")
    item["status"] = "rejected"
    write_json(approvals_path(), queue)
    log_decision("human", "manager_rejected_cancellation", {
        "id": item_id, "proposal_id": item.get("proposal_id"), "guest_id": item.get("guest_id"),
    })
    return item


def approve_reminder(item_id, env=None):
    """Send one PayPal reminder only after a fresh status check and manager approval."""
    queue = read_json(approvals_path(), [])
    reminder = _find_item(queue, item_id)
    if reminder.get("type") != "payment_reminder" or reminder.get("status") != "pending":
        raise ValueError("This reminder is no longer waiting for approval.")
    extension = _find_item(queue, reminder.get("proposal_id"))
    if extension.get("type") != "extension" or extension.get("status") != "sent" or not extension.get("invoice_id"):
        raise ValueError("The original extension has no eligible sent invoice.")
    env = env or load_env()
    try:
        status = read_invoice(env, extension["invoice_id"])["status"]
        if status in PAID:
            outcome = confirm_invoice_if_paid(extension["invoice_id"], env, source="reminder")
            if outcome is None or not outcome["confirmed"]:
                raise RuntimeError("PayPal payment status changed during confirmation.")
            queue = read_json(approvals_path(), [])
            reminder = _find_item(queue, item_id)
            reminder["status"] = "superseded"
            write_json(approvals_path(), queue)
            log_decision("human", "reminder_skipped_paid", {
                "id": item_id, "proposal_id": extension["id"], "guest_id": extension.get("guest_id"),
            })
            return {"status": "superseded", "item": reminder}
        if status not in {"SENT", "UNPAID"}:
            raise RuntimeError("PayPal invoice is not unpaid and sent.")
        send_reminder(env, extension["invoice_id"],
                      "Reminder: your stay extension invoice", reminder["message_draft"])
    except (Exception, SystemExit) as error:
        log_decision("human", "reminder_failed", {
            "id": item_id, "proposal_id": extension["id"],
            "guest_id": extension.get("guest_id"), "error_type": type(error).__name__,
        })
        raise
    reminder["status"] = "done"
    write_json(approvals_path(), queue)
    log_decision("human", "approved_reminder", {
        "id": item_id, "proposal_id": extension["id"], "guest_id": extension.get("guest_id"),
    })
    return {"status": "done", "item": reminder}


def reject_reminder(item_id):
    queue = read_json(approvals_path(), [])
    reminder = _find_item(queue, item_id)
    if reminder.get("type") != "payment_reminder" or reminder.get("status") != "pending":
        raise ValueError("This reminder is no longer waiting for approval.")
    reminder["status"] = "rejected"
    write_json(approvals_path(), queue)
    log_decision("human", "manager_rejected_reminder", {
        "id": item_id, "proposal_id": reminder.get("proposal_id"), "guest_id": reminder.get("guest_id"),
    })
    return reminder


def _same_usd_amount(payment_amount, expected):
    if not isinstance(payment_amount, dict) or payment_amount.get("currency_code") != "USD":
        return False
    try:
        return Decimal(str(payment_amount.get("value"))) == Decimal(str(expected))
    except (InvalidOperation, TypeError, ValueError):
        return False


def approve_refund(item_id, env=None):
    """A manager refunds a confirmed extension after a fresh official notice check."""
    queue = read_json(approvals_path(), [])
    refund = _find_item(queue, item_id)
    if refund.get("type") != "refund" or refund.get("status") != "pending":
        raise ValueError("This refund is no longer waiting for approval.")
    extension = _find_item(queue, refund.get("proposal_id"))
    if extension.get("type") != "extension" or extension.get("status") != "confirmed":
        raise ValueError("The original extension is no longer eligible for a refund.")
    if not extension.get("invoice_id") or refund.get("amount_usd") != extension.get("amount_usd"):
        raise ValueError("The saved invoice or refund amount is invalid.")

    guest = next((entry for entry in read_json(os.path.join(common.DATA_DIR, "guests.json"), {}).get("guests", [])
                  if entry.get("id") == extension.get("guest_id")), None)
    flight_number = guest.get("departure_flight") if guest else None
    flight_status = read_json(flights_path(), {}).get("flights", {}).get(flight_number, {}).get("status")
    if not flight_status or flight_status == "cancelled":
        log_decision("gate", "refund_blocked_notice_changed", {
            "id": item_id, "guest_id": extension.get("guest_id"),
            "flight": flight_number, "status": flight_status,
        })
        raise ValueError("The official notice no longer shows this guest’s flight as restored. No refund was issued.")

    env = env or load_env()
    try:
        payment = get_invoice_payment(env, extension["invoice_id"])
        if payment is None or not _same_usd_amount(payment.get("amount"), refund["amount_usd"]):
            raise RuntimeError("The PayPal payment does not match the full refund amount.")
        if payment.get("type") == "PAYPAL":
            capture_id = payment.get("payment_id")
            if not capture_id or not refund.get("refund_request_id"):
                raise RuntimeError("The PayPal capture or saved refund request ID is missing.")
            capture = get_capture(env, capture_id)
            if capture.get("status") != "COMPLETED":
                refund["status"] = "superseded"
                write_json(approvals_path(), queue)
                log_decision("human", "refund_skipped_not_refundable", {
                    "id": item_id, "proposal_id": extension["id"],
                    "guest_id": extension.get("guest_id"),
                })
                return {"status": "superseded", "item": refund}
            if not _same_usd_amount(capture.get("amount"), refund["amount_usd"]):
                raise RuntimeError("The PayPal capture does not match the full refund amount.")
            result = refund_capture(env, capture_id, refund["amount_usd"],
                                    refund["refund_request_id"], refund["message_draft"])
            method = "paypal"
        else:
            result = record_external_refund(env, extension["invoice_id"], refund["amount_usd"])
            method = "external"
    except (Exception, SystemExit) as error:
        log_decision("human", "refund_failed", {
            "id": item_id, "proposal_id": extension["id"],
            "guest_id": extension.get("guest_id"), "error_type": type(error).__name__,
        })
        raise

    extension["status"] = "refunded"
    refund["status"] = "done"
    refund["refund_id"] = result.get("refund_id")
    refund["method"] = method
    if method == "paypal":
        refund["refund_status"] = result["status"]
    write_json(approvals_path(), queue)
    log_decision("human", "approved_refund", {
        "id": item_id, "proposal_id": extension["id"],
        "guest_id": extension.get("guest_id"), "amount_usd": refund["amount_usd"],
        "method": method,
    })
    return {"status": "done", "item": refund}


def reject_refund(item_id):
    queue = read_json(approvals_path(), [])
    item = _find_item(queue, item_id)
    if item.get("type") != "refund" or item.get("status") != "pending":
        raise ValueError("This refund is no longer waiting for a decision.")
    item["status"] = "rejected"
    write_json(approvals_path(), queue)
    log_decision("human", "manager_rejected_refund", {
        "id": item_id, "proposal_id": item.get("proposal_id"), "guest_id": item.get("guest_id"),
    })
    return item


def defer_item(item_id):
    queue = read_json(approvals_path(), [])
    item = _find_item(queue, item_id)
    if item.get("status") != "pending":
        raise ValueError("This item is no longer waiting for a decision.")
    log_decision("human", "deferred", {"id": item_id, "type": item.get("type")})
    return item


def resolve_staff_note(item_id, handled=True):
    queue = read_json(approvals_path(), [])
    item = _find_item(queue, item_id)
    if item.get("type") != "staff_note" or item.get("status") != "pending":
        raise ValueError("This staff note is no longer waiting for a decision.")
    item["status"] = "acknowledged" if handled else "dismissed"
    write_json(approvals_path(), queue)
    log_decision("human", item["status"], {"id": item_id})
    return item


def main():
    queue = read_json(approvals_path(), [])
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
                    item, amount = approve_extension(item["id"], env=env)
                except RuntimeError as e:
                    print(f"  PayPal でエラーが起きました。\n  {e}")
                    current = _find_item(read_json(approvals_path(), []), item["id"])
                    if "invoice_id" not in current:
                        print("  この件は承認待ちのまま残します。")
                        continue
                    item = current
                    amount = item.get("amount_usd")
                if item["status"] == "sent":
                    print(f"  → 承認しました。請求書を客に送りました（ID: {item['invoice_id']}、合計 {amount} ドル）。")
                    print(f"    客が支払うページ: {item.get('pay_url') or '（PayPal の画面で確認してください）'}")
                else:
                    print(f"  → 承認しました。PayPal に請求書の下書きを作りました（ID: {item['invoice_id']}、合計 {amount} ドル）。")
            elif answer == "n":
                reject_extension(item["id"])
                print("  → 却下しました。何も実行していません。")
            else:
                defer_item(item["id"])
                print("  → あとで確認します。")
        elif item["type"] == "invoice_cancellation":
            print(f"[{item['id']}] 請求書の取り消し提案　{item['guest_name']} 様")
            print(f"  根拠　: {item['evidence']}")
            print(f"  AIの理由: {item['reason']}")
            print("  客に届く案内文:\n    " + item["message_draft"].replace("\n", "\n    "))
            answer = ask("\nPayPal の請求書を取り消しますか？ y / n / s : ")
            if answer == "y":
                try:
                    result = approve_cancellation(item["id"], env=env)
                    print("  → 支払い済みのため取り消していません。" if result["status"] == "superseded"
                          else "  → PayPal の請求書を取り消しました。")
                except (RuntimeError, SystemExit) as error:
                    print(f"  → 取り消しに失敗しました: {type(error).__name__}")
            elif answer == "n":
                reject_cancellation(item["id"])
                print("  → 却下しました。請求書はそのままです。")
            else:
                defer_item(item["id"])
                print("  → あとで確認します。")
        elif item["type"] == "payment_reminder":
            print(f"[{item['id']}] 支払い催促の提案　{item['guest_name']} 様")
            print(f"  AIの理由: {item['reason']}")
            print("  案内文の下書き:\n    " + item["message_draft"].replace("\n", "\n    "))
            answer = ask("\nPayPal から催促を送りますか？ y / n / s : ")
            if answer == "y":
                try:
                    result = approve_reminder(item["id"], env=env)
                    print("  → 支払い済みのため催促は送りませんでした。" if result["status"] == "superseded"
                          else "  → PayPal から催促を送りました。")
                except (RuntimeError, SystemExit) as error:
                    print(f"  → 催促に失敗しました: {type(error).__name__}")
            elif answer == "n":
                reject_reminder(item["id"])
                print("  → 却下しました。催促は送っていません。")
            else:
                defer_item(item["id"])
                print("  → あとで確認します。")
        elif item["type"] == "refund":
            print(f"[{item['id']}] 全額返金の提案　{item['guest_name']} 様（USD {item['amount_usd']:.2f}）")
            print(f"  根拠: {item['evidence']}")
            print(f"  AIの理由: {item['reason']}")
            print("  案内文の下書き:\n    " + item["message_draft"].replace("\n", "\n    "))
            answer = ask("\nPayPal Sandbox で返金しますか？ y / n / s : ")
            if answer == "y":
                try:
                    result = approve_refund(item["id"], env=env)
                    if result["status"] == "superseded":
                        print("  → 支払いが返金可能な状態ではないため返金しませんでした。")
                    elif result["item"].get("refund_status") == "PENDING":
                        print("  → PayPal Sandbox で返金処理中です。")
                    else:
                        print("  → PayPal Sandbox で返金しました。")
                except (RuntimeError, ValueError, SystemExit) as error:
                    print(f"  → 返金に失敗しました: {type(error).__name__}")
            elif answer == "n":
                reject_refund(item["id"])
                print("  → 却下しました。返金はしていません。")
            else:
                defer_item(item["id"])
                print("  → あとで確認します。")
        else:
            print(f"[{item['id']}] スタッフへの連絡　{item['guest_name']} 様")
            print(f"  内容: {item['note']}")
            answer = ask("\n確認しましたか？ y（確認済みにする）/ n（不要として閉じる）/ s（あとで）: ")
            if answer in ("y", "n"):
                resolve_staff_note(item["id"], handled=answer == "y")
            else:
                defer_item(item["id"])
        print()

    print("すべての確認が終わりました。判断の記録は logs/decision_log.jsonl に残っています。")


if __name__ == "__main__":
    main()
