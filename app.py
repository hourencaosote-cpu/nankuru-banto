"""責任者向け Web 画面。状態を変える操作はすべて POST に限定する。"""

import hmac
import json
import os
import secrets
import shutil
import threading
from datetime import datetime

from flask import Flask, abort, flash, redirect, render_template, request, session, url_for

import agent
from approve import approve_extension, defer_item, reject_extension, resolve_staff_note
from check_payments import check_payments as check_payments_action
from common import APPROVALS_PATH, DATA_DIR, LOG_PATH, load_env, log_decision, read_json, write_json


app = Flask(__name__)
app.config.update(
    SECRET_KEY=os.environ.get("SECRET_KEY") or secrets.token_hex(32),
    MAX_CONTENT_LENGTH=16 * 1024,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Strict",
    SESSION_COOKIE_SECURE="RENDER" in os.environ,
)

ACTION_LOCK = threading.Lock()
ACTIVE_EXTENSION_STATUSES = {"pending", "approved", "sent", "confirmed"}

STATUS_LABELS = {
    "pending": "Approval pending",
    "approved": "Invoice draft",
    "sent": "Invoice sent",
    "confirmed": "Paid · extension confirmed",
    "rejected": "Proposal rejected",
    "acknowledged": "Handled by staff",
    "dismissed": "Closed",
}

ACTION_LABELS = {
    "proposed_extension": "Stay extension proposed",
    "rejected_proposal": "Safety gate blocked a proposal",
    "duplicate_staff_note": "Duplicate staff follow-up blocked",
    "notified_staff": "Staff follow-up created",
    "finished": "AI review completed",
    "approved_extension": "Manager approved an extension",
    "sent_extension_invoice": "PayPal invoice sent",
    "approval_failed": "Invoice creation failed safely",
    "rejected_extension": "Manager rejected a proposal",
    "deferred": "Decision deferred",
    "acknowledged": "Staff note handled",
    "dismissed": "Staff note closed",
    "payment_confirmed": "Payment confirmed",
    "payment_pending": "Payment still pending",
    "payment_check_failed": "Payment check failed safely",
    "blocked_unknown_tool": "Unknown AI tool blocked",
    "stopped_max_steps": "AI stopped at its step limit",
    "ai_check_failed": "AI check failed safely",
    "web_action_failed": "Web action failed safely",
    "reset_demo": "Demo state archived and reset",
}

GATE_RULES = [
    "Official cancellation required",
    "Direct bookings only",
    "Only the nights needed, up to 3",
    "Extension-room inventory cannot be exceeded",
    "No duplicate proposal for the same guest",
    "Guest message draft is required",
]


def _csrf_token():
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


@app.context_processor
def inject_csrf_token():
    return {"csrf_token": _csrf_token}


@app.before_request
def protect_post_requests():
    if request.method != "POST":
        return
    expected = session.get("csrf_token", "")
    submitted = request.form.get("csrf_token", "")
    if not expected or not submitted or not hmac.compare_digest(expected, submitted):
        abort(400)


@app.template_filter("money")
def format_money(value):
    return f"{float(value or 0):,.2f}"


@app.template_filter("short_time")
def format_short_time(value, as_of=None, base_time=None):
    try:
        display_time = datetime.fromisoformat(value)
        if as_of is not None and base_time is not None:
            display_time = datetime.fromisoformat(as_of) + (
                display_time - datetime.fromisoformat(base_time)
            )
        return f"{display_time.strftime('%b')} {display_time.day} · {display_time.strftime('%H:%M')}"
    except (TypeError, ValueError):
        return value or "Unknown time"


def _read_log_entries():
    if not os.path.exists(LOG_PATH):
        return []
    entries = []
    with open(LOG_PATH, encoding="utf-8") as log_file:
        for line in log_file:
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return entries


def _gate_reason(reason):
    translations = (
        ("OTA経由", "OTA bookings cannot be billed directly by the inn. Staff follow-up is required."),
        ("空室がもうありません", "No extension rooms remain. Staff follow-up is required."),
        ("便は欠航ではありません", "The official notice does not show a cancellation."),
        ("宿泊客IDは存在しません", "The guest ID does not exist."),
        ("延泊は1〜", "The requested stay is outside the 1–3 night limit."),
        ("必要なのは", "The request exceeds the nights needed for the next available flight."),
        ("案内文の下書き", "A guest message draft is required."),
        ("すでに承認待ち", "A proposal already exists for this guest."),
    )
    for marker, translation in translations:
        if marker in reason:
            return translation
    return "The proposal did not satisfy a safety-gate rule."


def _guest_response(guest, items, flight_status):
    guest_items = [item for item in items if item.get("guest_id") == guest.get("id")]
    extensions = [item for item in guest_items if item.get("type") == "extension"]
    staff_notes = [item for item in guest_items if item.get("type") == "staff_note"]
    if extensions:
        status = extensions[-1].get("status", "pending")
        return STATUS_LABELS.get(status, status.replace("_", " ").title()), status
    if staff_notes:
        status = staff_notes[-1].get("status", "pending")
        label = "Staff follow-up needed" if status == "pending" else STATUS_LABELS.get(status, status.title())
        return label, "staff" if status == "pending" else status
    if flight_status == "cancelled":
        return "Awaiting AI review", "warning"
    return "No action needed", "neutral"


def _log_view(entry, flights, guest_count, guest_names):
    detail = entry.get("detail") if isinstance(entry.get("detail"), dict) else {}
    if entry.get("action") == "tool_call":
        name = detail.get("name")
        args = detail.get("args") if isinstance(detail.get("args"), dict) else {}
        if name == "get_flight_status":
            flight_number = args.get("flight", "Unknown flight")
            status = flights.get(flight_number, {}).get("status", "not found").replace("_", " ")
            label = f"Checked official flight {flight_number} → {status}"
            subject = "Official notice"
        elif name == "get_social_posts":
            label = "Read social posts → unverified, ignored"
            subject = "Not used for decisions"
        elif name == "list_guests":
            label = f"Checked guest roster → {guest_count} guests"
            subject = "Guest records"
        else:
            label = "AI used an investigation tool"
            subject = str(name or "Tool")
        return {
            "time": entry.get("time", ""),
            "actor": "AI",
            "label": label,
            "subject": subject,
        }

    guest_id = detail.get("guest_id")
    if entry.get("action") == "rejected_proposal":
        subject = guest_names.get(guest_id) or guest_id or "System"
    else:
        subject = guest_names.get(guest_id) or detail.get("guest_name") or detail.get("id") or guest_id or "System"
    return {
        "time": entry.get("time", ""),
        "actor": str(entry.get("actor", "system")).upper(),
        "label": ACTION_LABELS.get(entry.get("action"), str(entry.get("action", "Activity")).replace("_", " ").title()),
        "subject": subject,
    }


def build_dashboard():
    guest_data = read_json(os.path.join(DATA_DIR, "guests.json"), {})
    flight_data = read_json(os.path.join(DATA_DIR, "flights.json"), {})
    approvals = read_json(APPROVALS_PATH, [])
    log_entries = _read_log_entries()
    base_time = next(
        (entry.get("time") for entry in reversed(log_entries) if entry.get("action") == "reset_demo" and entry.get("time")),
        log_entries[0].get("time") if log_entries else None,
    )
    flights = flight_data.get("flights", {})
    guests = guest_data.get("guests", [])
    guest_names = {guest.get("id"): guest.get("name", "Unknown guest") for guest in guests}

    guest_rows = []
    cancelled_count = 0
    for guest in guests:
        flight = flights.get(guest.get("departure_flight"), {})
        status = flight.get("status", "unknown")
        if status == "cancelled":
            cancelled_count += 1
        response, response_style = _guest_response(guest, approvals, status)
        guest_rows.append(
            {
                **guest,
                "flight_status": status,
                "flight_date": flight.get("date", ""),
                "official_source": flight.get("official_source", ""),
                "response": response,
                "response_style": response_style,
            }
        )

    room_capacity = int(guest_data.get("vacant_rooms_for_extension", 0))
    allocated_rooms = sum(
        1
        for item in approvals
        if item.get("type") == "extension" and item.get("status") in ACTIVE_EXTENSION_STATUSES
    )
    paid_revenue = sum(
        float(item.get("amount_usd", 0))
        for item in approvals
        if item.get("type") == "extension" and item.get("status") == "confirmed"
    )
    unpaid_revenue = sum(
        float(item.get("amount_usd", 0))
        for item in approvals
        if item.get("type") == "extension" and item.get("status") in {"approved", "sent"}
    )

    gate_events = []
    seen_guests = set()
    for entry in reversed(log_entries[-80:]):
        if entry.get("action") != "rejected_proposal":
            continue
        detail = entry.get("detail", {})
        guest_id = detail.get("guest_id", "Unknown guest")
        if guest_id in seen_guests:
            continue
        seen_guests.add(guest_id)
        gate_events.append(
            {
                "guest_name": guest_names.get(guest_id, "Unknown guest"),
                "time": entry.get("time", ""),
                "reasons": [_gate_reason(reason) for reason in detail.get("reasons", [])],
            }
        )
        if len(gate_events) == 4:
            break

    status_order = {"pending": 0, "approved": 1, "sent": 2, "confirmed": 3, "rejected": 4}
    proposals = sorted(
        [item for item in approvals if item.get("type") == "extension"],
        key=lambda item: (status_order.get(item.get("status"), 9), item.get("created_at", "")),
    )
    staff_notes = sorted(
        [item for item in approvals if item.get("type") == "staff_note"],
        key=lambda item: (item.get("status") != "pending", item.get("created_at", "")),
    )

    return {
        "hotel": guest_data.get("hotel", "Nankuru Demo Inn"),
        "as_of": flight_data.get("as_of", "Unknown"),
        "log_base_time": base_time,
        "typhoon_status": "Typhoon response active" if cancelled_count else "No official cancellations",
        "metrics": {
            "checkouts": len(guests),
            "cancelled": cancelled_count,
            "rooms_remaining": max(room_capacity - allocated_rooms, 0),
            "room_capacity": room_capacity,
            "paid_revenue": paid_revenue,
            "unpaid_revenue": unpaid_revenue,
        },
        "guests": guest_rows,
        "proposals": proposals,
        "staff_notes": staff_notes,
        "gate_events": gate_events,
        "gate_rules": GATE_RULES,
        "social_posts": flight_data.get("unverified_social_posts", []),
        "activity": [_log_view(entry, flights, len(guests), guest_names) for entry in reversed(log_entries[-18:])],
        "status_labels": STATUS_LABELS,
    }


def archive_demo_state():
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    archive_dir = os.path.join(DATA_DIR, "archive", timestamp)
    os.makedirs(archive_dir, exist_ok=False)

    if not os.path.exists(APPROVALS_PATH):
        write_json(APPROVALS_PATH, [])
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    if not os.path.exists(LOG_PATH):
        open(LOG_PATH, "w", encoding="utf-8").close()

    log_decision("human", "reset_demo", {"archive": f"data/archive/{timestamp}"})
    for source in (APPROVALS_PATH, LOG_PATH):
        shutil.move(source, os.path.join(archive_dir, os.path.basename(source)))

    write_json(APPROVALS_PATH, [])
    open(LOG_PATH, "w", encoding="utf-8").close()
    return timestamp


def _record_web_failure(action, error):
    log_decision("system", "web_action_failed", {"action": action, "error_type": type(error).__name__})


@app.get("/")
def dashboard():
    return render_template("dashboard.html", **build_dashboard())


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.post("/run-ai")
def run_ai_check():
    try:
        with ACTION_LOCK:
            env = load_env()
            agent.run_agent(agent.call_gemini, env)
    except (SystemExit, Exception) as error:
        log_decision("system", "ai_check_failed", {"error_type": type(error).__name__})
        flash("The AI check could not finish. No approval was performed. Please try again shortly.", "error")
    else:
        flash("AI check completed. Review the proposals before approving anything.", "success")
    return redirect(url_for("dashboard") + "#review-queue")


@app.post("/reset-demo")
def reset_demo():
    try:
        with ACTION_LOCK:
            archive_demo_state()
    except OSError as error:
        _record_web_failure("reset_demo", error)
        flash("The demo could not be reset. Existing data was left in place where possible.", "error")
    else:
        flash("Demo reset. Previous files were archived. PayPal invoices were not changed.", "success")
    return redirect(url_for("dashboard"))


@app.post("/proposals/<item_id>/approve")
def approve_proposal(item_id):
    try:
        with ACTION_LOCK:
            item, _amount = approve_extension(item_id)
    except ValueError as error:
        flash(str(error), "error")
    except (SystemExit, RuntimeError) as error:
        _record_web_failure("approve_extension", error)
        flash("PayPal could not create or send the invoice. The saved status is shown below; no duplicate invoice will be created.", "error")
    else:
        if item.get("status") == "sent":
            flash("Approved. The PayPal sandbox invoice was sent.", "success")
        else:
            flash("Approved. A PayPal sandbox invoice draft was created; no email was sent.", "success")
    return redirect(url_for("dashboard") + "#review-queue")


@app.post("/proposals/<item_id>/reject")
def reject_proposal(item_id):
    try:
        with ACTION_LOCK:
            reject_extension(item_id)
    except ValueError as error:
        flash(str(error), "error")
    else:
        flash("Proposal rejected. No invoice was created.", "success")
    return redirect(url_for("dashboard") + "#review-queue")


@app.post("/items/<item_id>/later")
def review_later(item_id):
    try:
        defer_item(item_id)
    except ValueError as error:
        flash(str(error), "error")
    else:
        flash("Kept in the review queue for later.", "info")
    return redirect(url_for("dashboard") + "#review-queue")


@app.post("/staff-notes/<item_id>/handle")
def handle_staff_note(item_id):
    try:
        with ACTION_LOCK:
            resolve_staff_note(item_id, handled=True)
    except ValueError as error:
        flash(str(error), "error")
    else:
        flash("Staff follow-up marked as handled.", "success")
    return redirect(url_for("dashboard") + "#review-queue")


@app.post("/check-payments")
def check_payments():
    try:
        with ACTION_LOCK:
            result = check_payments_action()
    except (SystemExit, RuntimeError) as error:
        _record_web_failure("check_payments", error)
        flash("PayPal could not be reached. Existing invoice statuses were left unchanged.", "error")
    else:
        confirmed = len(result["newly_confirmed"])
        pending = len(result["pending"])
        failed = len(result["errors"])
        if confirmed:
            flash(f"Payment check complete: {confirmed} extension(s) confirmed, {pending} still pending.", "success")
        elif result["checked"] or failed:
            flash(f"Payment check complete: {pending} still pending, {failed} could not be checked.", "info")
        else:
            flash("There are no sent invoices waiting for payment.", "info")
    return redirect(url_for("dashboard") + "#review-queue")


@app.errorhandler(400)
def bad_request(_error):
    return render_template(
        "error.html",
        title="Request not accepted",
        message="This form expired or could not be verified. Return to the dashboard and try again.",
    ), 400


@app.errorhandler(404)
def not_found(_error):
    return render_template(
        "error.html",
        title="Page not found",
        message="The requested page does not exist.",
    ), 404


if __name__ == "__main__":
    # macOS は 5000 番を AirPlay が使っていて「アクセス拒否（403）」になるため 5001 番を使う
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", "5001")), debug=False)
