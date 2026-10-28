"""One-time local Sandbox recording helper. Run manually; never from the web app.

This intentionally creates and sends real PayPal Sandbox invoices. It does not
approve the later cancellation proposal; that remains a manager decision.
"""

import os

import agent
from approve import approve_extension
from common import approvals_path, load_env, log_path, read_json, write_json


def main():
    env = load_env()
    if not env.get("BUYER_EMAIL"):
        raise SystemExit("Sandbox の BUYER_EMAIL が必要です。")
    write_json(approvals_path(), [])
    os.makedirs(os.path.dirname(log_path()), exist_ok=True)
    with open(log_path(), "w", encoding="utf-8"):
        pass
    previous_notice = os.environ.get("NANKURU_NOTICE")
    os.environ["NANKURU_NOTICE"] = "initial"

    try:
        agent.run_ai(env, record=True)
        kim = next(item for item in read_json(approvals_path(), [])
                   if item.get("type") == "extension" and item.get("guest_id") == "G002"
                   and item.get("status") == "pending")
        approved, _ = approve_extension(kim["id"], env=env)
        if approved["status"] != "sent":
            raise RuntimeError("Kim の請求書が送付済みになりませんでした。")
        emily = next(item for item in read_json(approvals_path(), [])
                     if item.get("type") == "extension" and item.get("guest_id") == "G001"
                     and item.get("status") == "pending")
        approved, _ = approve_extension(emily["id"], env=env)
        if approved["status"] != "sent":
            raise RuntimeError("Emily の請求書が送付済みになりませんでした。")
        os.environ["NANKURU_NOTICE"] = "update"
        agent.run_ai(env, record=True, recording_path=agent.UPDATE_REPLAY_PATH,
                     recording_required_type="cancellation_and_reminder")
    except (Exception, SystemExit) as error:
        raise SystemExit(f"録画を中断しました（{type(error).__name__}）。状態を確認してください。") from None
    finally:
        if previous_notice is None:
            os.environ.pop("NANKURU_NOTICE", None)
        else:
            os.environ["NANKURU_NOTICE"] = previous_notice

    items = read_json(approvals_path(), [])
    cancellations = sum(item.get("type") == "invoice_cancellation" for item in items)
    reminders = sum(item.get("type") == "payment_reminder" for item in items)
    print(f"初回の延泊提案を録画し、Kim と Emily の Sandbox 請求書を送付しました。更新後の取り消し提案 {cancellations} 件と催促提案 {reminders} 件を録画しました。")
    print("取り消しの承認は行っていません。")


if __name__ == "__main__":
    main()
