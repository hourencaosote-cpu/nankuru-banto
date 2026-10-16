"""
共通の部品：設定ファイル（env.txt）の読み込みと、データ・ログの置き場所。
"""

import json
import os
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "data")
LOG_DIR = os.path.join(HERE, "logs")
APPROVALS_PATH = os.path.join(DATA_DIR, "approvals.json")
LOG_PATH = os.path.join(LOG_DIR, "decision_log.jsonl")
ENV_KEYS = (
    "PAYPAL_CLIENT_ID",
    "PAYPAL_CLIENT_SECRET",
    "BUYER_EMAIL",
    "GEMINI_API_KEY",
    "GEMINI_MODEL",
    "SECRET_KEY",
    "INVOICE_BUSINESS_NAME",
)


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
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def log_decision(actor, action, detail):
    """判断ログ：誰が（AI／人）、何を、どんな理由でしたかを1行ずつ残す。"""
    os.makedirs(LOG_DIR, exist_ok=True)
    entry = {
        "time": datetime.now().isoformat(timespec="seconds"),
        "actor": actor,
        "action": action,
        "detail": detail,
    }
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
