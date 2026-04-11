#!/usr/bin/env python3
"""Send Telegram meal reminders at fixed checkpoints (Notion-backed)."""

from __future__ import annotations

import json
import os
import sys
import re
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
_NOTION_DB_ID_RE = re.compile(r"[0-9a-fA-F]{32}|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


def _require_env(name: str, value: str) -> None:
    if not value:
        print(f"缺少环境变量: {name}")
        sys.exit(1)


def _normalize_notion_database_id(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if raw.startswith("collection://"):
        return raw.removeprefix("collection://").strip()
    match = _NOTION_DB_ID_RE.search(raw)
    if match:
        candidate = match.group(0)
        if len(candidate) == 32:
            return f"{candidate[0:8]}-{candidate[8:12]}-{candidate[12:16]}-{candidate[16:20]}-{candidate[20:32]}"
        return candidate
    return raw


def _notion_request_json(endpoint: str, payload: dict | None = None, method: str = "GET") -> dict:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        f"https://api.notion.com/v1{endpoint}",
        data=data,
        headers={
            "Authorization": f"Bearer {NOTION_TOKEN}",
            "Content-Type": "application/json",
            "Notion-Version": "2022-06-28",
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="ignore") if exc.fp else ""
        raise RuntimeError(f"HTTP {exc.code}: {body or exc.reason}") from exc


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


def _notion_database_schema(database_id: str) -> dict:
    return _notion_request_json(f"/databases/{database_id}", method="GET")


def _notion_pick_property(schema: dict, prop_type: str, keywords: tuple[str, ...] = ()) -> str | None:
    props = schema.get("properties") if isinstance(schema, dict) else {}
    if not isinstance(props, dict):
        return None
    candidates: list[str] = []
    for name, prop in props.items():
        if not isinstance(prop, dict) or prop.get("type") != prop_type:
            continue
        candidates.append(name)
        if keywords and any(keyword in name for keyword in keywords):
            return name
    if len(candidates) == 1:
        return candidates[0]
    return None


def _notion_query_today_meals() -> int:
    database_id = _normalize_notion_database_id(NOTION_MEALS_DB)
    schema = _notion_database_schema(database_id)
    date_prop = _notion_pick_property(schema, "date", ("时间", "日期"))
    if not date_prop:
        raise RuntimeError("Meals 数据库缺少日期字段，或字段类型不是 date")
    tz = ZoneInfo(TZ_NAME or "Asia/Shanghai")
    now = datetime.now(tz)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    payload = {
        "filter": {
            "property": date_prop,
            "date": {
                "on_or_after": start.isoformat(),
                "before": end.isoformat(),
            },
        }
    }
    result = _post_json(
        f"https://api.notion.com/v1/databases/{database_id}/query",
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
    _require_env("NOTION_MEALS_DB", _normalize_notion_database_id(NOTION_MEALS_DB))
    _require_env("TELEGRAM_BOT_TOKEN", TELEGRAM_BOT_TOKEN)
    _require_env("TELEGRAM_CHAT_ID", TELEGRAM_CHAT_ID)

    meal_count = None
    meal_line = "当前未能读取今日进食记录，请手动确认是否已记录。"
    try:
        meal_count = _notion_query_today_meals()
        meal_line = f"今日已记录进食 {meal_count} 次。"
    except Exception as exc:  # noqa: BLE001
        print(f"警告: Notion 查询失败，改为发送通用提醒: {exc}")

    tz = ZoneInfo(TZ_NAME or "Asia/Shanghai")
    now = datetime.now(tz).strftime("%Y-%m-%d %H:%M")
    message = (
        f"提醒：请在进食窗口 {REMINDER_WINDOW} 内完成下一餐。\n"
        f"{meal_line} ({now})"
    )
    _send_telegram(message)
    print("已发送提醒。")


if __name__ == "__main__":
    main()
