#!/usr/bin/env python3
"""Send Telegram meal reminders at fixed checkpoints (Notion-backed)."""

from __future__ import annotations

import json
import os
import sys
import urllib.request
import urllib.error
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


NOTION_TOKEN = os.getenv("NOTION_TOKEN", "").strip()
NOTION_MEALS_DB = os.getenv("NOTION_MEALS_DB", "").strip()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
REMINDER_WINDOW = os.getenv("REMINDER_WINDOW", "07:00-15:00").strip()
TZ_NAME = os.getenv("TZ", "Asia/Shanghai").strip()


def _require_env(name: str, value: str) -> None:
    if not value:
        print(f"缺少环境变量: {name}")
        sys.exit(1)


def _post_json(url: str, payload: dict, headers: dict) -> dict:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="ignore") if exc.fp else ""
        raise RuntimeError(f"HTTP {exc.code}: {body or exc.reason}") from exc


def _notion_query_today_meals() -> int:
    tz = ZoneInfo(TZ_NAME or "Asia/Shanghai")
    now = datetime.now(tz)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    payload = {
        "filter": {
            "property": "时间",
            "date": {
                "on_or_after": start.isoformat(),
                "before": end.isoformat(),
            },
        }
    }
    result = _post_json(
        f"https://api.notion.com/v1/databases/{NOTION_MEALS_DB}/query",
        payload,
        headers={
            "Authorization": f"Bearer {NOTION_TOKEN}",
            "Content-Type": "application/json",
            "Notion-Version": "2022-06-28",
        },
    )
    return len(result.get("results", []))


def _send_telegram(message: str) -> None:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "disable_web_page_preview": True,
    }
    _post_json(url, payload, headers={"Content-Type": "application/json"})


def main() -> None:
    _require_env("NOTION_TOKEN", NOTION_TOKEN)
    _require_env("NOTION_MEALS_DB", NOTION_MEALS_DB)
    _require_env("TELEGRAM_BOT_TOKEN", TELEGRAM_BOT_TOKEN)
    _require_env("TELEGRAM_CHAT_ID", TELEGRAM_CHAT_ID)

    meal_count = _notion_query_today_meals()

    tz = ZoneInfo(TZ_NAME or "Asia/Shanghai")
    now = datetime.now(tz).strftime("%Y-%m-%d %H:%M")
    message = (
        f"提醒：请在进食窗口 {REMINDER_WINDOW} 内完成下一餐。\n"
        f"今日已记录进食 {meal_count} 次。({now})"
    )
    _send_telegram(message)
    print("已发送提醒。")


if __name__ == "__main__":
    main()
