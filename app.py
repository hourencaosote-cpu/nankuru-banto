"""責任者向け Web 画面。状態を変える操作はすべて POST に限定する。"""

import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import threading
import time
from datetime import datetime, timezone
from functools import wraps

from flask import Flask, abort, flash, g, redirect, render_template, request, session, url_for
from werkzeug.exceptions import RequestEntityTooLarge

import agent
from approve import approve_extension, defer_item, reject_extension, resolve_staff_note
from check_payments import check_payments as check_payments_action, confirm_invoice_if_paid
from common import (
    DATA_DIR, approvals_path, load_env, log_decision, log_path, read_json,
    reset_workspace_paths, set_workspace_paths, write_json,
)
from paypal_tools import record_invoice_payment, verify_webhook_signature


app = Flask(__name__)
app.config.update(
    SECRET_KEY=os.environ.get("SECRET_KEY") or secrets.token_hex(32),
    MAX_CONTENT_LENGTH=16 * 1024,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Strict",
    SESSION_COOKIE_SECURE="RENDER" in os.environ,
)

WORKSPACE_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
WORKSPACE_LOCKS = {}
ACTIVE_REQUESTS = {}
WORKSPACE_STATE_LOCK = threading.Lock()
INVOICE_INDEX_LOCK = threading.Lock()
LIMITS_LOCK = threading.Lock()
AI_SEMAPHORE = threading.BoundedSemaphore(2)
WORKSPACE_TTL_SECONDS = 24 * 60 * 60
MAX_WORKSPACES = 500
LIMIT_DEFAULTS = {
    "MAX_AI_RUNS_PER_HOUR_PER_VISITOR": 5,
    "MAX_AI_RUNS_PER_DAY": 50,
    "MAX_INVOICES_PER_DAY_PER_VISITOR": 10,
    "MAX_INVOICES_PER_DAY": 200,
    "MAX_PAYMENT_RECORDS_PER_DAY_PER_VISITOR": 10,
}
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
    "draft_rewrite_requested": "Draft rewrite requested",
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
    "payment_confirmed_webhook": "Payment confirmed by PayPal webhook",
    "payment_pending": "Payment still pending",
    "payment_check_failed": "Payment check failed safely",
    "recorded_sandbox_payment": "Manager recorded sandbox payment",
    "blocked_unknown_tool": "Unknown AI tool blocked",
    "stopped_max_steps": "AI stopped at its step limit",
    "ai_check_failed": "AI check failed safely",
    "web_action_failed": "Web action failed safely",
    "reset_demo": "Demo state archived and reset",
    "rate_limit_blocked": "Demo rate limit reached",
    "ai_capacity_blocked": "AI capacity is busy",
    "workspace_busy": "Previous action still running",
    "replay_mode": "Demo replay used recorded Gemini responses",
    "live_run_rolled_back": "Live AI run rolled back",
    "gemini_model_used": "Gemini model responded",
}

GATE_RULES = [
    "Official cancellation required",
    "Direct bookings only",
    "Only the nights needed, up to 3",
    "Extension-room inventory cannot be exceeded",
    "No duplicate proposal for the same guest",
    "Guest message draft is required",
]


class DemoLimitReached(Exception):
    def __init__(self, message, operation, scope):
        super().__init__(message)
        self.operation = operation
        self.scope = scope


class InvoiceIndexError(RuntimeError):
    """A saved invoice could not be mapped to its visitor workspace."""


def _workspaces_dir():
    return os.path.join(DATA_DIR, "workspaces")


def _invoice_index_path():
    return os.path.join(_workspaces_dir(), "_invoice_index.json")


def _remember_invoice(invoice_id, workspace_id):
    if not isinstance(invoice_id, str) or not invoice_id:
        raise InvoiceIndexError("An invoice ID is required for automatic payment updates.")
    with INVOICE_INDEX_LOCK:
        index = read_json(_invoice_index_path(), {})
        previous = index.get(invoice_id)
        if previous is not None and previous != workspace_id:
            raise InvoiceIndexError("Invoice ID already belongs to another workspace.")
        if previous != workspace_id:
            index[invoice_id] = workspace_id
            write_json(_invoice_index_path(), index)


def _workspace_for_invoice(invoice_id):
    with INVOICE_INDEX_LOCK:
        workspace_id = read_json(_invoice_index_path(), {}).get(invoice_id)
    if isinstance(workspace_id, str) and WORKSPACE_ID_PATTERN.fullmatch(workspace_id):
        return workspace_id
    return None


def _forget_invoice_workspaces(workspace_ids):
    with INVOICE_INDEX_LOCK:
        index = read_json(_invoice_index_path(), {})
        kept = {invoice_id: workspace_id for invoice_id, workspace_id in index.items()
                if workspace_id not in workspace_ids}
        if len(kept) != len(index):
            write_json(_invoice_index_path(), kept)


def _cleanup_workspaces_locked(now=None):
    root = _workspaces_dir()
    if not os.path.isdir(root):
        return
    now = time.time() if now is None else now
    remaining = []
    protected_count = 0
    deleted_ids = set()
    for entry in os.scandir(root):
        if not WORKSPACE_ID_PATTERN.fullmatch(entry.name) or not entry.is_dir(follow_symlinks=False):
            continue
        workspace_lock = WORKSPACE_LOCKS.get(entry.name)
        if ACTIVE_REQUESTS.get(entry.name, 0) or (workspace_lock is not None and workspace_lock.locked()):
            protected_count += 1
            continue
        modified = entry.stat(follow_symlinks=False).st_mtime
        if now - modified >= WORKSPACE_TTL_SECONDS:
            shutil.rmtree(entry.path)
            WORKSPACE_LOCKS.pop(entry.name, None)
            deleted_ids.add(entry.name)
        else:
            remaining.append((modified, entry))
    excess = max(0, len(remaining) + protected_count - MAX_WORKSPACES)
    for _, entry in sorted(remaining, key=lambda pair: pair[0])[:excess]:
        shutil.rmtree(entry.path)
        WORKSPACE_LOCKS.pop(entry.name, None)
        deleted_ids.add(entry.name)
    if deleted_ids:
        _forget_limit_visitors(deleted_ids)
        _forget_invoice_workspaces(deleted_ids)


def cleanup_workspaces(now=None):
    with WORKSPACE_STATE_LOCK:
        _cleanup_workspaces_locked(now)


def _new_workspace_id(root):
    while True:
        workspace_id = secrets.token_hex(16)
        if not os.path.lexists(os.path.join(root, workspace_id)):
            return workspace_id


@app.before_request
def bind_workspace():
    if request.endpoint == "paypal_webhook":
        request.max_content_length = 64 * 1024
        return
    if request.endpoint in {"healthz", "api_state"}:
        return
    with WORKSPACE_STATE_LOCK:
        root = _workspaces_dir()
        workspace_id = session.get("workspace_id")
        valid = isinstance(workspace_id, str) and WORKSPACE_ID_PATTERN.fullmatch(workspace_id) is not None
        workspace_dir = os.path.join(root, workspace_id) if valid else None
        expired = (
            valid and os.path.isdir(workspace_dir) and not os.path.islink(workspace_dir)
            and time.time() - os.path.getmtime(workspace_dir) >= WORKSPACE_TTL_SECONDS
        )
        created = False
        if not valid or not os.path.isdir(workspace_dir) or os.path.islink(workspace_dir) or expired:
            _cleanup_workspaces_locked()
            os.makedirs(root, exist_ok=True)
            workspace_id = _new_workspace_id(root)
            workspace_dir = os.path.join(root, workspace_id)
            session["workspace_id"] = workspace_id
            created = True
        os.makedirs(workspace_dir, exist_ok=True)
        os.utime(workspace_dir, None)
        ACTIVE_REQUESTS[workspace_id] = ACTIVE_REQUESTS.get(workspace_id, 0) + 1
        g.workspace_id = workspace_id
        g.workspace_lock = WORKSPACE_LOCKS.setdefault(workspace_id, threading.Lock())
        g.path_tokens = set_workspace_paths(workspace_dir)
        if created:
            _cleanup_workspaces_locked()


@app.teardown_request
def unbind_workspace(_error):
    tokens = getattr(g, "path_tokens", None)
    if tokens is None:
        return
    reset_workspace_paths(tokens)
    workspace_id = g.workspace_id
    with WORKSPACE_STATE_LOCK:
        ACTIVE_REQUESTS[workspace_id] -= 1
        if not ACTIVE_REQUESTS[workspace_id]:
            del ACTIVE_REQUESTS[workspace_id]


def _limit_value(name):
    try:
        return max(0, int(os.environ.get(name, LIMIT_DEFAULTS[name])))
    except ValueError:
        return LIMIT_DEFAULTS[name]


def _limits_path():
    return os.path.join(_workspaces_dir(), "_limits.json")


def _load_limits(day, now):
    limits = read_json(_limits_path(), {})
    visitors = limits.setdefault("visitors", {})
    if limits.get("day") != day:
        limits["day"] = day
        limits["ai_total"] = 0
        limits["invoice_total"] = 0
        for workspace_id, visitor in list(visitors.items()):
            visitor["invoice_total"] = 0
            visitor["payment_records_total"] = 0
            visitor["ai_times"] = [stamp for stamp in visitor.get("ai_times", []) if stamp > now - 3600]
            if not visitor["ai_times"] and not visitor["payment_records_total"]:
                del visitors[workspace_id]
    limits.setdefault("ai_total", 0)
    limits.setdefault("invoice_total", 0)
    return limits


def _save_limits(limits):
    write_json(_limits_path(), limits)


def _forget_limit_visitors(workspace_ids):
    with LIMITS_LOCK:
        limits = read_json(_limits_path(), {})
        visitors = limits.get("visitors", {})
        removed = False
        for workspace_id in workspace_ids:
            if workspace_id in visitors:
                del visitors[workspace_id]
                removed = True
        if removed:
            _save_limits(limits)


def _reserve_limit(operation, workspace_id):
    now = time.time()
    day = datetime.fromtimestamp(now, timezone.utc).date().isoformat()
    with LIMITS_LOCK:
        limits = _load_limits(day, now)
        visitor = limits["visitors"].setdefault(
            workspace_id, {"ai_times": [], "invoice_total": 0, "payment_records_total": 0},
        )
        if operation == "ai":
            visitor["ai_times"] = [stamp for stamp in visitor.get("ai_times", []) if stamp > now - 3600]
            if len(visitor["ai_times"]) >= _limit_value("MAX_AI_RUNS_PER_HOUR_PER_VISITOR"):
                raise DemoLimitReached("You have reached the AI demo limit for this hour. Please try again later.", operation, "visitor_hour")
            if limits["ai_total"] >= _limit_value("MAX_AI_RUNS_PER_DAY"):
                raise DemoLimitReached("Demo limit reached for today. Please try again tomorrow.", operation, "global_day")
            visitor["ai_times"].append(now)
            limits["ai_total"] += 1
        elif operation == "payment_record":
            if visitor.get("payment_records_total", 0) >= _limit_value("MAX_PAYMENT_RECORDS_PER_DAY_PER_VISITOR"):
                raise DemoLimitReached(
                    "You have reached today's sandbox payment recording limit. Please try again tomorrow.",
                    operation, "visitor_day",
                )
            visitor["payment_records_total"] = visitor.get("payment_records_total", 0) + 1
        elif operation == "invoice":
            if visitor.get("invoice_total", 0) >= _limit_value("MAX_INVOICES_PER_DAY_PER_VISITOR"):
                raise DemoLimitReached("You have reached today's invoice demo limit. Please try again tomorrow.", operation, "visitor_day")
            if limits["invoice_total"] >= _limit_value("MAX_INVOICES_PER_DAY"):
                raise DemoLimitReached("Demo invoice limit reached for today. Please try again tomorrow.", operation, "global_day")
            visitor["invoice_total"] = visitor.get("invoice_total", 0) + 1
            limits["invoice_total"] += 1
        else:
            raise ValueError("Unknown demo limit operation.")
        _save_limits(limits)
    return day


def _release_invoice_limit(workspace_id, day):
    with LIMITS_LOCK:
        limits = read_json(_limits_path(), {})
        if limits.get("day") != day:
            return
        visitor = limits.get("visitors", {}).get(workspace_id, {})
        visitor["invoice_total"] = max(0, visitor.get("invoice_total", 0) - 1)
        limits["invoice_total"] = max(0, limits.get("invoice_total", 0) - 1)
        _save_limits(limits)


def _release_payment_record_limit(workspace_id, day):
    with LIMITS_LOCK:
        limits = read_json(_limits_path(), {})
        if limits.get("day") != day:
            return
        visitor = limits.get("visitors", {}).get(workspace_id, {})
        visitor["payment_records_total"] = max(0, visitor.get("payment_records_total", 0) - 1)
        _save_limits(limits)


class InvoiceQuota:
    def __init__(self, workspace_id):
        self.workspace_id = workspace_id
        self.day = None
        self.created = False

    def reserve(self):
        self.day = _reserve_limit("invoice", self.workspace_id)

    def mark_created(self):
        self.created = True

    def release_if_unused(self):
        if self.day is not None and not self.created:
            _release_invoice_limit(self.workspace_id, self.day)


cleanup_workspaces()


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
    if request.method != "POST" or request.endpoint == "paypal_webhook":
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
    path = log_path()
    entries = []
    try:
        with open(path, encoding="utf-8") as log_file:
            for line in log_file:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except FileNotFoundError:
        return []
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


def _tracker_data(guest_rows, approvals):
    tracker_statuses = {
        "pending": ("Pending approval", "pending"),
        "approved": ("Pending approval", "approved"),
        "sent": ("Invoice sent", "sent"),
        "confirmed": ("Paid · confirmed", "confirmed"),
        "rejected": ("Rejected", "rejected"),
        "staff": ("Staff follow-up", "approved"),
        "warning": ("Awaiting AI review", "pending"),
        "neutral": ("No action needed", "approved"),
    }
    rows = []
    for guest in guest_rows:
        guest_items = [item for item in approvals if item.get("guest_id") == guest.get("id")]
        extension = next((item for item in reversed(guest_items) if item.get("type") == "extension"), None)
        status, status_style = tracker_statuses.get(
            guest["response_style"], (guest["response"], guest["response_style"]),
        )

        flight_number = guest.get("departure_flight", "")
        flight_status = guest.get("flight_status", "unknown")
        invoice_id = str(extension.get("invoice_id") or "") if extension else ""
        rows.append({
            "guest": guest.get("name", ""),
            "room": guest.get("room", ""),
            "booking": "OTA" if guest.get("booking_channel") == "ota" else "Direct",
            "flight": f"{flight_number} · {flight_status.replace('_', ' ').capitalize()}",
            "proposalNights": extension.get("nights") if extension else None,
            "amountUsd": float(extension.get("amount_usd") or 0) if extension else 0.0,
            "status": status,
            "statusStyle": status_style,
            "invoice": invoice_id[-6:] if invoice_id else "—",
        })
    return {
        "rows": rows,
        "totals": {
            "guest": "Proposal total",
            "proposalNights": sum(row["proposalNights"] or 0 for row in rows),
            "amountUsd": round(sum(row["amountUsd"] for row in rows), 2),
        },
    }


def _departure_labels(guests, as_of):
    try:
        checkout_date = datetime.fromisoformat(guests[0]["checkout_date"]).date()
        notice_date = datetime.fromisoformat(as_of).date()
    except (IndexError, KeyError, TypeError, ValueError):
        return "guests scheduled to depart", "UPCOMING DEPARTURES"

    date_label = f"{checkout_date.strftime('%b')} {checkout_date.day}"
    days_until = (checkout_date - notice_date).days
    relative_day = {0: "TODAY", 1: "TOMORROW"}.get(days_until, "UPCOMING")
    return f"guests departing {date_label}", f"{relative_day} · {date_label.upper()}"


def _activity_views(entries, flights, guest_count, guest_names):
    views = []
    latest_model = None
    for entry in entries:
        action = entry.get("action")
        if action == "gemini_model_used":
            detail = entry.get("detail") if isinstance(entry.get("detail"), dict) else {}
            latest_model = detail.get("model")
            continue
        view = _log_view(entry, flights, guest_count, guest_names)
        if action == "finished":
            if latest_model:
                view["model"] = latest_model
            latest_model = None
        elif action in {"ai_check_failed", "replay_mode", "reset_demo"}:
            latest_model = None
        views.append(view)
    return list(reversed(views[-18:]))


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
    action = entry.get("action")
    label_action = "payment_confirmed_webhook" if (
        action == "payment_confirmed" and detail.get("via") == "webhook"
    ) else action
    view = {
        "time": entry.get("time", ""),
        "actor": str(entry.get("actor", "system")).upper(),
        "label": ACTION_LABELS.get(label_action, str(action or "Activity").replace("_", " ").title()),
        "subject": subject,
    }
    if entry.get("action") == "ai_check_failed":
        view["diagnostic"] = detail.get("code") if detail.get("code") is not None else detail.get("error_type")
    return view


def _approval_state(approvals):
    pairs = [(item.get("id"), item.get("status")) for item in approvals]
    version = hashlib.sha256(json.dumps(pairs, sort_keys=True).encode("utf-8")).hexdigest()[:16]
    sent = sum(1 for item in approvals if item.get("type") == "extension" and item.get("status") == "sent")
    return {"version": version, "sent": sent}


def build_dashboard():
    guest_data = read_json(os.path.join(DATA_DIR, "guests.json"), {})
    flight_data = read_json(os.path.join(DATA_DIR, "flights.json"), {})
    approvals = read_json(approvals_path(), [])
    log_entries = _read_log_entries()
    last_ai_source = next(
        (entry.get("action") for entry in reversed(log_entries)
         if entry.get("action") in {"replay_mode", "gemini_model_used"}),
        None,
    )
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
        if not approvals and not log_entries:
            response, response_style = "Awaiting AI review", "warning"
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

    checkout_caption, departure_eyebrow = _departure_labels(guests, flight_data.get("as_of"))

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
        "checkout_caption": checkout_caption,
        "departure_eyebrow": departure_eyebrow,
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
        "activity": _activity_views(log_entries, flights, len(guests), guest_names),
        "status_labels": STATUS_LABELS,
        "tracker_data": _tracker_data(guest_rows, approvals),
        "payment_state": _approval_state(approvals),
        "replay_active": last_ai_source == "replay_mode",
    }


def archive_demo_state():
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    approvals_file = approvals_path()
    log_file = log_path()
    archive_dir = os.path.join(os.path.dirname(approvals_file), "archive", timestamp)
    os.makedirs(archive_dir, exist_ok=False)

    if not os.path.exists(approvals_file):
        write_json(approvals_file, [])
    os.makedirs(os.path.dirname(log_file), exist_ok=True)
    if not os.path.exists(log_file):
        open(log_file, "w", encoding="utf-8").close()

    log_decision("human", "reset_demo", {"archive": f"archive/{timestamp}"})
    for source in (approvals_file, log_file):
        shutil.move(source, os.path.join(archive_dir, os.path.basename(source)))

    write_json(approvals_file, [])
    open(log_file, "w", encoding="utf-8").close()
    return timestamp


def _record_web_failure(action, error):
    log_decision("system", "web_action_failed", {"action": action, "error_type": type(error).__name__})


def workspace_action(action):
    def decorate(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if not g.workspace_lock.acquire(blocking=False):
                log_decision("system", "workspace_busy", {"action": action})
                flash("Your previous action is still running. Please wait a moment and refresh.", "error")
                return redirect(url_for("dashboard") + "#review-queue")
            try:
                return view(*args, **kwargs)
            finally:
                g.workspace_lock.release()
        return wrapped
    return decorate


@app.get("/")
def dashboard():
    return render_template("dashboard.html", **build_dashboard())


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/api/state")
def api_state():
    """Read the current visitor's approval version without changing any state."""
    workspace_id = session.get("workspace_id")
    if not isinstance(workspace_id, str) or not WORKSPACE_ID_PATTERN.fullmatch(workspace_id):
        return {"error": "Workspace not found"}, 404
    workspace_dir = os.path.join(_workspaces_dir(), workspace_id)
    if not os.path.isdir(workspace_dir) or os.path.islink(workspace_dir):
        return {"error": "Workspace not found"}, 404
    tokens = set_workspace_paths(workspace_dir)
    try:
        return _approval_state(read_json(approvals_path(), []))
    finally:
        reset_workspace_paths(tokens)


@app.post("/webhooks/paypal")
def paypal_webhook():
    """Use a verified PayPal notice to trigger a fresh invoice-status lookup."""
    try:
        env = load_env()
        if not env.get("PAYPAL_WEBHOOK_ID"):
            log_decision("system", "webhook_not_configured", {})
            return {"status": "unavailable"}, 503

        event = request.get_json(silent=True)
        if not isinstance(event, dict):
            return {"status": "invalid"}, 400
        if not verify_webhook_signature(env, request.headers, event):
            return {"status": "invalid"}, 400
        if event.get("event_type") != "INVOICING.INVOICE.PAID":
            return {"status": "ignored"}, 200

        resource = event.get("resource")
        invoice = resource.get("invoice") if isinstance(resource, dict) else None
        invoice_id = invoice.get("id") if isinstance(invoice, dict) else None
        if not isinstance(invoice_id, str) or not invoice_id:
            invoice_id = resource.get("id") if isinstance(resource, dict) else None
        if not isinstance(invoice_id, str) or not invoice_id:
            return {"status": "ignored"}, 200
        workspace_id = _workspace_for_invoice(invoice_id)
        if workspace_id is None:
            return {"status": "ignored"}, 200

        with WORKSPACE_STATE_LOCK:
            workspace_dir = os.path.join(_workspaces_dir(), workspace_id)
            if not os.path.isdir(workspace_dir) or os.path.islink(workspace_dir):
                return {"status": "ignored"}, 200
            workspace_lock = WORKSPACE_LOCKS.setdefault(workspace_id, threading.Lock())
            ACTIVE_REQUESTS[workspace_id] = ACTIVE_REQUESTS.get(workspace_id, 0) + 1
        try:
            if not workspace_lock.acquire(timeout=10):
                return {"status": "busy"}, 503
            try:
                tokens = set_workspace_paths(workspace_dir)
                try:
                    item = next(
                        (entry for entry in read_json(approvals_path(), [])
                         if entry.get("type") == "extension" and entry.get("invoice_id") == invoice_id),
                        None,
                    )
                    if item is None:
                        return {"status": "ignored"}, 200
                    if item.get("status") == "confirmed":
                        log_decision("paypal", "webhook_received", {
                            "invoice_id": invoice_id, "result": "already_confirmed",
                        })
                        return {"status": "ok"}, 200
                    if item.get("status") != "sent":
                        return {"status": "ignored"}, 200

                    for attempt in range(3):
                        outcome = confirm_invoice_if_paid(invoice_id, env, source="webhook")
                        if outcome is not None and outcome["confirmed"]:
                            log_decision("paypal", "webhook_received", {
                                "invoice_id": invoice_id, "result": "confirmed",
                            })
                            return {"status": "ok"}, 200
                        if attempt < 2:
                            time.sleep(2)
                    log_decision("paypal", "webhook_received", {
                        "invoice_id": invoice_id, "result": "not_paid_yet",
                    })
                    return {"status": "not paid yet"}, 503
                finally:
                    reset_workspace_paths(tokens)
            finally:
                workspace_lock.release()
        finally:
            with WORKSPACE_STATE_LOCK:
                ACTIVE_REQUESTS[workspace_id] -= 1
                if not ACTIVE_REQUESTS[workspace_id]:
                    del ACTIVE_REQUESTS[workspace_id]
    except RequestEntityTooLarge:
        return {"status": "too large"}, 413
    except (SystemExit, Exception) as error:
        log_decision("system", "webhook_failed", {"error_type": type(error).__name__})
        return {"status": "unavailable"}, 503


@app.post("/run-ai")
@workspace_action("run_ai_check")
def run_ai_check():
    try:
        if not AI_SEMAPHORE.acquire(blocking=False):
            log_decision("system", "ai_capacity_blocked", {})
            flash("Other visitors are running the AI right now. Please try again in a minute.", "error")
            return redirect(url_for("dashboard") + "#review-queue")
        try:
            env = load_env()
            counted = False

            def limited_gemini(model_env, contents):
                nonlocal counted
                if not counted:
                    _reserve_limit("ai", g.workspace_id)
                    counted = True
                return agent.call_gemini(model_env, contents)

            try:
                agent.run_ai(env, call_model=limited_gemini, web=True)
            except DemoLimitReached as limit_error:
                try:
                    agent.run_ai(
                        env, mode="replay", web=True,
                        replay_reason=f"AI demo limit ({limit_error.scope})",
                    )
                except agent.GeminiUnavailable:
                    raise limit_error from None
        finally:
            AI_SEMAPHORE.release()
    except DemoLimitReached as error:
        log_decision("system", "rate_limit_blocked", {"operation": error.operation, "scope": error.scope})
        flash(str(error), "error")
    except agent.GeminiUnavailable as error:
        log_decision("system", "ai_check_failed", {
            "code": error.code, "error_type": error.cause_type or "GeminiUnavailable",
        })
        if error.code == 429:
            message = "Gemini usage limit reached (429). No approval was performed."
        elif error.code == 503:
            message = "Gemini is busy right now (503). No approval was performed."
        elif error.code is None:
            message = "Gemini is unavailable or not configured. No approval was performed."
        else:
            message = f"Gemini returned an error ({error.code}). No approval was performed."
        flash(message, "error")
    except (SystemExit, Exception) as error:
        log_decision("system", "ai_check_failed", {"error_type": type(error).__name__})
        sanitized = RuntimeError("Exception details withheld")
        app.logger.exception(
            "AI check failed unexpectedly (%s)", type(error).__name__,
            exc_info=(RuntimeError, sanitized, error.__traceback__),
        )
        flash("The AI check could not finish. No approval was performed. Please try again shortly.", "error")
    else:
        flash("AI check completed. Review the proposals before approving anything.", "success")
    return redirect(url_for("dashboard") + "#review-queue")


@app.post("/reset-demo")
@workspace_action("reset_demo")
def reset_demo():
    try:
        archive_demo_state()
    except OSError as error:
        _record_web_failure("reset_demo", error)
        flash("The demo could not be reset. Existing data was left in place where possible.", "error")
    else:
        flash("Demo reset. Previous files were archived. PayPal invoices were not changed.", "success")
    return redirect(url_for("dashboard"))


@app.post("/proposals/<item_id>/approve")
@workspace_action("approve_proposal")
def approve_proposal(item_id):
    quota = InvoiceQuota(g.workspace_id)
    index_failed = False

    def remember_invoice(invoice_id):
        nonlocal index_failed
        try:
            _remember_invoice(invoice_id, g.workspace_id)
        except (Exception, SystemExit) as error:
            index_failed = True
            log_decision("system", "web_action_failed", {
                "action": "invoice_index", "error_type": type(error).__name__,
            })

    try:
        try:
            item, _amount = approve_extension(
                item_id, before_create=quota.reserve, on_invoice_created=quota.mark_created,
                on_invoice_ready=remember_invoice,
            )
        finally:
            quota.release_if_unused()
    except DemoLimitReached as error:
        log_decision("system", "rate_limit_blocked", {"operation": error.operation, "scope": error.scope})
        flash(str(error), "error")
    except ValueError as error:
        flash(str(error), "error")
    except (SystemExit, RuntimeError, OSError) as error:
        _record_web_failure("approve_extension", error)
        flash("PayPal could not create or send the invoice. The saved status is shown below; no duplicate invoice will be created.", "error")
    else:
        if item.get("status") == "sent":
            message = "Approved. The PayPal sandbox invoice was sent."
            if index_failed:
                message += " Automatic payment updates are unavailable for this invoice. Use Check payments now."
            flash(message, "success")
        else:
            flash("Approved. A PayPal sandbox invoice draft was created; no email was sent.", "success")
    return redirect(url_for("dashboard") + "#review-queue")


@app.post("/proposals/<item_id>/reject")
@workspace_action("reject_proposal")
def reject_proposal(item_id):
    try:
        reject_extension(item_id)
    except ValueError as error:
        flash(str(error), "error")
    else:
        flash("Proposal rejected. No invoice was created.", "success")
    return redirect(url_for("dashboard") + "#review-queue")


@app.post("/items/<item_id>/later")
@workspace_action("review_later")
def review_later(item_id):
    try:
        defer_item(item_id)
    except ValueError as error:
        flash(str(error), "error")
    else:
        flash("Kept in the review queue for later.", "info")
    return redirect(url_for("dashboard") + "#review-queue")


@app.post("/staff-notes/<item_id>/handle")
@workspace_action("handle_staff_note")
def handle_staff_note(item_id):
    try:
        resolve_staff_note(item_id, handled=True)
    except ValueError as error:
        flash(str(error), "error")
    else:
        flash("Staff follow-up marked as handled.", "success")
    return redirect(url_for("dashboard") + "#review-queue")


@app.post("/check-payments")
@workspace_action("check_payments")
def check_payments():
    try:
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


@app.post("/proposals/<item_id>/record-sandbox-payment")
@workspace_action("record_sandbox_payment")
def record_sandbox_payment(item_id):
    queue = read_json(approvals_path(), [])
    item = next((entry for entry in queue if entry.get("id") == item_id), None)
    if (item is None or item.get("type") != "extension" or item.get("status") != "sent"
            or not item.get("invoice_id") or item.get("payment_recorded_by_manager")):
        flash("This invoice is not eligible for sandbox payment recording.", "error")
        return redirect(url_for("dashboard") + "#review-queue")

    try:
        day = _reserve_limit("payment_record", g.workspace_id)
    except DemoLimitReached as error:
        log_decision("system", "rate_limit_blocked", {"operation": error.operation, "scope": error.scope})
        flash(str(error), "error")
        return redirect(url_for("dashboard") + "#review-queue")

    try:
        env = load_env()
        record_invoice_payment(env, item["invoice_id"], item["amount_usd"])
    except (RuntimeError, SystemExit, ValueError, OSError) as error:
        _release_payment_record_limit(g.workspace_id, day)
        _record_web_failure("record_sandbox_payment", error)
        flash("PayPal could not record the sandbox payment. The invoice is still marked as sent.", "error")
        return redirect(url_for("dashboard") + "#review-queue")

    item["payment_recorded_by_manager"] = True
    write_json(approvals_path(), queue)
    log_decision("human", "recorded_sandbox_payment", {"id": item_id, "invoice_id": item["invoice_id"]})
    try:
        check_payments_action(env=env)
    except (RuntimeError, SystemExit) as error:
        _record_web_failure("confirm_recorded_sandbox_payment", error)
        flash("Sandbox payment was recorded. PayPal status could not be checked; use Check payments to confirm it.", "error")
    else:
        current = next(
            (entry for entry in read_json(approvals_path(), []) if entry.get("id") == item_id), None,
        )
        if current and current.get("status") == "confirmed":
            flash("Sandbox payment recorded by the manager. The extension is confirmed.", "success")
        else:
            flash("Sandbox payment was recorded. Use Check payments to confirm the invoice status.", "info")
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


@app.errorhandler(405)
def method_not_allowed(error):
    if request.method == "GET":
        flash("The server just woke up. Please press the button again.", "info")
        return redirect(url_for("dashboard"))
    return error


if __name__ == "__main__":
    # macOS は 5000 番を AirPlay が使っていて「アクセス拒否（403）」になるため 5001 番を使う
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", "5001")), debug=False)
