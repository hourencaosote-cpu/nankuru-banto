"""
「安全装置」の最後の関門：責任者（人）が承認待ちリストを確認し、承認した提案だけを実行する。

- 延泊の提案：承認すると、PayPal sandbox に請求書が作られ、客（テスト用の買い手）に送られる。
  客が請求書を支払えば延泊が確定する。env.txt の BUYER_EMAIL が空なら下書きを作るだけ。
- スタッフへの連絡：内容を確認して「確認済み」にするだけ。お金は動かない。

使い方:  python3 approve.py
"""

from common import approvals_path, load_env, log_decision, read_json, write_json
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


def _find_item(queue, item_id):
    for item in queue:
        if item.get("id") == item_id:
            return item
    raise ValueError(f"Approval item '{item_id}' was not found.")


def approve_extension(item_id, env=None, before_create=None, on_invoice_created=None):
    """人が承認した延泊だけを PayPal sandbox の請求書にする。"""
    queue = read_json(approvals_path(), [])
    item = _find_item(queue, item_id)
    if item.get("type") != "extension":
        raise ValueError("Only stay-extension proposals can be approved here.")
    if item.get("status") not in ("pending", "approved"):
        raise ValueError("This proposal is no longer waiting for approval.")

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
