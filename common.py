"""
共通の部品：設定ファイル（env.txt）の読み込みと、データ・ログの置き場所。
"""

import json
import os
import tempfile
from contextvars import ContextVar
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "data")
LOG_DIR = os.path.join(HERE, "logs")
_approvals_override = ContextVar("approvals_path", default=None)
_log_override = ContextVar("log_path", default=None)
ENV_KEYS = (
    "PAYPAL_CLIENT_ID",
    "PAYPAL_CLIENT_SECRET",
    "BUYER_EMAIL",
    "GEMINI_API_KEY",
    "GEMINI_MODEL",
    "SECRET_KEY",
    "INVOICE_BUSINESS_NAME",
    "AI_RUN_BUDGET_SECONDS",
    "GEMINI_FALLBACK_MODELS",
    "AI_MODE",
)


def approvals_path():
    return _approvals_override.get() or os.path.join(DATA_DIR, "approvals.json")


def log_path():
    return _log_override.get() or os.path.join(LOG_DIR, "decision_log.jsonl")


def set_workspace_paths(workspace_dir):
    """Bind one visitor's files to the current request; return reset tokens."""
    return (
        _approvals_override.set(os.path.join(workspace_dir, "approvals.json")),
        _log_override.set(os.path.join(workspace_dir, "decision_log.jsonl")),
    )


def reset_workspace_paths(tokens):
    _approvals_override.reset(tokens[0])
    _log_override.reset(tokens[1])


def load_env():
    """環境変数を読み、同じフォルダの env.txt（なければ .env）を優先する。"""
    env = {key: os.environ[key] for key in ENV_KEYS if key in os.environ}
    for name in ("env.txt", ".env"):
        path = os.path.join(HERE, name)
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, value = line.split("=", 1)
                    env[key.strip()] = value.strip().strip('"').strip("'")
            return env
    return env


def read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def write_json(path, data):
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=directory, delete=False) as f:
            temporary_path = f.name
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None and os.path.exists(temporary_path):
            os.remove(temporary_path)


def log_decision(actor, action, detail):
    """判断ログ：誰が（AI／人）、何を、どんな理由でしたかを1行ずつ残す。"""
    path = log_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    entry = {
        "time": datetime.now().isoformat(timespec="seconds"),
        "actor": actor,
        "action": action,
        "detail": detail,
    }
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
