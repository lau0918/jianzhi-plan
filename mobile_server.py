#!/usr/bin/env python3
"""Mobile-friendly web server for 8+16 fasting tracker."""

from __future__ import annotations

import argparse
import json
import os
import re
import urllib.error
import urllib.request
from urllib.parse import parse_qs, urlparse
from dataclasses import asdict
from datetime import datetime, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict

from fasting_tracker import (
    DATE_FMT,
    TIME_FMT,
    DEFAULT_PLAN,
    DEFAULT_DATA,
    MealRecord,
    Record,
    WeightRecord,
    SleepRecord,
    ExerciseRecord,
    calc_streak,
    evaluate_day,
    hours_between,
    is_meal_in_window,
    load_data,
    now_local,
    parse_clock,
    parse_time,
    period_stats,
    goal_stats,
    save_data,
    weight_stats,
)

WEB_DIR = Path(__file__).parent / "web"
AUTH_TOKEN = os.getenv("AUTH_TOKEN", "").strip()
NOTION_TOKEN = os.getenv("NOTION_TOKEN", "").strip()
NOTION_MEALS_DB = os.getenv("NOTION_MEALS_DB", "").strip()
NOTION_WEIGHTS_DB = os.getenv("NOTION_WEIGHTS_DB", "").strip()
NOTION_GOALS_DB = os.getenv("NOTION_GOALS_DB", "").strip()
NOTION_SLEEP_DB = os.getenv("NOTION_SLEEP_DB", "").strip()
NOTION_EXERCISE_DB = os.getenv("NOTION_EXERCISE_DB", "").strip()
_NOTION_DB_SCHEMA_CACHE: Dict[str, Dict[str, Any]] = {}
_NOTION_DB_ID_RE = re.compile(r"[0-9a-fA-F]{32}|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_NOTION_DB_RESOLUTION_CACHE: Dict[str, str] = {}
_NOTION_RESET_TARGETS = (
    ("进食记录", lambda: NOTION_MEALS_DB),
    ("体重记录", lambda: NOTION_WEIGHTS_DB),
    ("目标设置", lambda: NOTION_GOALS_DB),
    ("睡眠记录", lambda: NOTION_SLEEP_DB),
    ("运动记录", lambda: NOTION_EXERCISE_DB),
)


def _notion_enabled() -> bool:
    return bool(
        NOTION_TOKEN
        and (
            NOTION_MEALS_DB
            or NOTION_WEIGHTS_DB
            or NOTION_GOALS_DB
            or NOTION_SLEEP_DB
            or NOTION_EXERCISE_DB
        )
    )


def _notion_datetime(value: str) -> str:
    # value: "YYYY-MM-DD HH:MM"
    return value.replace(" ", "T") + ":00"


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


def _notion_readable_error(exc: Exception, database_id: str, env_name: str) -> str:
    message = str(exc) or "未知错误"
    lowered = message.lower()
    if "object_not_found" in lowered or "could not find database" in lowered:
        return f"{env_name} 不可访问：请检查 ID 或 Share 权限"
    if "unauthorized" in lowered or "restricted_resource" in lowered:
        return f"{env_name} 未授权：请检查 Share 权限"
    return message


def _notion_search_database_id(title: str) -> str | None:
    if not NOTION_TOKEN:
        return None
    payload = {
        "query": title,
        "filter": {"property": "object", "value": "database"},
        "page_size": 10,
    }
    try:
        result = _notion_request_json("/search", payload=payload, method="POST")
    except Exception:
        return None
    for item in result.get("results", []):
        if not isinstance(item, dict) or item.get("object") != "database":
            continue
        db_title = ((item.get("title") or [])[:1] or [{}])[0].get("plain_text", "")
        if db_title == title:
            return str(item.get("id") or "").strip()
    return None


def _resolve_notion_database_id(value: str, title: str) -> str:
    cache_key = f"{title}:{value}"
    if cache_key in _NOTION_DB_RESOLUTION_CACHE:
        return _NOTION_DB_RESOLUTION_CACHE[cache_key]
    normalized = _normalize_notion_database_id(value)
    if normalized:
        try:
            _notion_database_schema(normalized)
            _NOTION_DB_RESOLUTION_CACHE[cache_key] = normalized
            return normalized
        except Exception:
            pass
    fallback = _notion_search_database_id(title)
    if fallback:
        _NOTION_DB_RESOLUTION_CACHE[cache_key] = fallback
        return fallback
    _NOTION_DB_RESOLUTION_CACHE[cache_key] = normalized
    return normalized


def _notion_request_json(endpoint: str, payload: Dict[str, Any] | None = None, method: str = "GET") -> Dict[str, Any]:
    if not NOTION_TOKEN:
        return {}
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
        with urllib.request.urlopen(req, timeout=8) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "ignore")
        raise RuntimeError(raw or f"{exc.code} {exc.reason}") from exc


def _notion_database_schema(database_id: str) -> Dict[str, Any]:
    normalized = _normalize_notion_database_id(database_id)
    if normalized in _NOTION_DB_SCHEMA_CACHE:
        return _NOTION_DB_SCHEMA_CACHE[normalized]
    schema = _notion_request_json(f"/databases/{normalized}", method="GET")
    _NOTION_DB_SCHEMA_CACHE[normalized] = schema
    return schema


def _notion_properties(schema: Dict[str, Any]) -> Dict[str, Any]:
    props = schema.get("properties")
    return props if isinstance(props, dict) else {}


def _notion_pick_property(schema: Dict[str, Any], prop_type: str, keywords: tuple[str, ...] = ()) -> str | None:
    props = _notion_properties(schema)
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


def _notion_read_property(prop: Dict[str, Any] | None) -> Any:
    if not prop:
        return None
    kind = prop.get("type")
    if kind == "title":
        parts = prop.get("title") or []
        return "".join(str(item.get("plain_text") or "") for item in parts).strip() or None
    if kind == "rich_text":
        parts = prop.get("rich_text") or []
        return "".join(str(item.get("plain_text") or "") for item in parts).strip() or None
    if kind == "date":
        date = prop.get("date") or {}
        return date.get("start")
    if kind == "number":
        return prop.get("number")
    if kind == "select":
        select = prop.get("select") or {}
        return select.get("name")
    if kind == "multi_select":
        values = prop.get("multi_select") or []
        return [item.get("name") for item in values if isinstance(item, dict) and item.get("name")]
    if kind == "checkbox":
        return prop.get("checkbox")
    return None


def _coerce_text_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        parts = re.split(r"[、,，|/]\s*", value)
        return [part.strip() for part in parts if part.strip()]
    text = str(value).strip()
    return [text] if text else []


def _notion_sync_page(database_id: str, properties: Dict[str, Any]) -> tuple[bool, str | None]:
    normalized = _normalize_notion_database_id(database_id)
    if not NOTION_TOKEN or not normalized:
        return False, "未配置 Notion"
    payload = {"parent": {"database_id": normalized}, "properties": properties}
    try:
        _notion_request_json("/pages", payload=payload, method="POST")
        return True, None
    except Exception as exc:
        message = _notion_readable_error(exc, normalized, "数据库")
        print(f"警告: Notion 写入失败: {message}")
        return False, message


def _notion_update_page(page_id: str, properties: Dict[str, Any]) -> tuple[bool, str | None]:
    normalized = str(page_id or "").strip()
    if not NOTION_TOKEN or not normalized:
        return False, "未配置 Notion"
    try:
        _notion_request_json(f"/pages/{normalized}", payload={"properties": properties}, method="PATCH")
        return True, None
    except Exception as exc:
        message = str(exc) or "未知错误"
        print(f"警告: Notion 更新失败: {message}")
        return False, message


def _notion_find_meal_page_id(db_id: str, title_prop: str, time_prop: str, meal: MealRecord) -> str | None:
    try:
        payload = {
            "page_size": 1,
            "filter": {
                "and": [
                    {"property": title_prop, "title": {"equals": meal.food}},
                    {"property": time_prop, "date": {"equals": _notion_datetime(meal.time)}},
                ]
            },
            "sorts": [{"timestamp": "last_edited_time", "direction": "descending"}],
        }
        result = _notion_request_json(f"/databases/{db_id}/query", payload=payload, method="POST")
        rows = result.get("results") or []
        if rows and isinstance(rows[0], dict):
            return str(rows[0].get("id") or "").strip() or None
    except Exception:
        return None
    return None


def _notion_query_all_page_ids(database_id: str) -> list[str]:
    page_ids: list[str] = []
    start_cursor: str | None = None
    while True:
        payload: Dict[str, Any] = {"page_size": 100, "sorts": [{"timestamp": "last_edited_time", "direction": "descending"}]}
        if start_cursor:
            payload["start_cursor"] = start_cursor
        result = _notion_request_json(f"/databases/{database_id}/query", payload=payload, method="POST")
        for item in result.get("results", []):
            if isinstance(item, dict) and item.get("id"):
                page_ids.append(str(item["id"]))
        if not result.get("has_more"):
            break
        start_cursor = str(result.get("next_cursor") or "").strip() or None
        if not start_cursor:
            break
    return page_ids


def _notion_archive_database(database_id: str) -> tuple[int, str | None]:
    ids = _notion_query_all_page_ids(database_id)
    archived = 0
    for page_id in ids:
        try:
            _notion_request_json(f"/pages/{page_id}", payload={"archived": True}, method="PATCH")
            archived += 1
        except Exception as exc:
            return archived, _notion_readable_error(exc, database_id, "数据库")
    return archived, None


def _reset_local_data() -> None:
    save_data(json.loads(json.dumps(DEFAULT_DATA)))


def _resolve_reset_database_ids() -> Dict[str, str]:
    resolved: Dict[str, str] = {}
    for title, getter in _NOTION_RESET_TARGETS:
        value = getter()
        if not value:
            continue
        db_id = _resolve_notion_database_id(value, title)
        if db_id:
            resolved[title] = db_id
    return resolved


def _notion_sync_meal(meal: MealRecord, in_window: bool, prefer_update: bool = False) -> tuple[bool, str | None]:
    db_id = _resolve_notion_database_id(NOTION_MEALS_DB, "进食记录")
    if not db_id:
        return False, "未配置进食表"
    try:
        schema = _notion_database_schema(db_id)
    except Exception as exc:
        return False, f"读取进食表结构失败: {_notion_readable_error(exc, db_id, 'NOTION_MEALS_DB')}"
    title_prop = _notion_pick_property(schema, "title", ("食物", "名称", "记录"))
    time_prop = _notion_pick_property(schema, "date", ("时间", "日期"))
    window_prop = _notion_pick_property(schema, "select", ("窗口状态", "状态", "类型", "分类"))
    amount_prop = _notion_pick_property(schema, "select", ("餐量", "份量", "分量"))
    amount_text_prop = _notion_pick_property(schema, "rich_text", ("餐量", "份量", "分量"))
    diet_multi_prop = _notion_pick_property(schema, "multi_select", ("饮食类型", "饮食", "标签"))
    diet_text_prop = _notion_pick_property(schema, "rich_text", ("饮食类型", "饮食", "标签"))
    risk_multi_prop = _notion_pick_property(schema, "multi_select", ("风险场景", "场景", "风险"))
    risk_text_prop = _notion_pick_property(schema, "rich_text", ("风险场景", "场景", "风险"))
    note_prop = _notion_pick_property(schema, "rich_text", ("备注", "说明", "描述"))
    if not title_prop or not time_prop:
        return False, "进食表缺少标题或时间字段"
    diet_types = _coerce_text_list(getattr(meal, "diet_types", []))
    risk_scenarios = _coerce_text_list(getattr(meal, "risk_scenarios", []))
    props = {
        title_prop: {"title": [{"text": {"content": meal.food}}]},
        time_prop: {"date": {"start": _notion_datetime(meal.time)}},
    }
    if window_prop:
        props[window_prop] = {"select": {"name": "窗口内" if in_window else "窗口外"}}
    if amount_prop:
        props[amount_prop] = {"select": {"name": meal.meal_amount or "正常"}}
    elif amount_text_prop:
        props[amount_text_prop] = {"rich_text": [{"text": {"content": meal.meal_amount or "正常"}}]}
    if diet_types:
        if diet_multi_prop:
            props[diet_multi_prop] = {"multi_select": [{"name": item} for item in diet_types]}
        elif diet_text_prop:
            props[diet_text_prop] = {"rich_text": [{"text": {"content": "、".join(diet_types)}}]}
    if risk_scenarios:
        if risk_multi_prop:
            props[risk_multi_prop] = {"multi_select": [{"name": item} for item in risk_scenarios]}
        elif risk_text_prop:
            props[risk_text_prop] = {"rich_text": [{"text": {"content": "、".join(risk_scenarios)}}]}
    if note_prop:
        props[note_prop] = {"rich_text": [{"text": {"content": meal.note or ""}}]}
    if prefer_update:
        page_id = _notion_find_meal_page_id(db_id, title_prop, time_prop, meal)
        if page_id:
            return _notion_update_page(page_id, props)
    return _notion_sync_page(db_id, props)


def _notion_sync_weight(item: WeightRecord) -> tuple[bool, str | None]:
    db_id = _resolve_notion_database_id(NOTION_WEIGHTS_DB, "体重记录")
    if not db_id:
        return False, "未配置体重表"
    try:
        schema = _notion_database_schema(db_id)
    except Exception as exc:
        return False, f"读取体重表结构失败: {_notion_readable_error(exc, db_id, 'NOTION_WEIGHTS_DB')}"
    title_prop = _notion_pick_property(schema, "title", ("体重", "记录", "重量"))
    time_prop = _notion_pick_property(schema, "date", ("时间", "日期"))
    weight_prop = _notion_pick_property(schema, "number", ("体重", "重量", "数值"))
    note_prop = _notion_pick_property(schema, "rich_text", ("备注", "说明", "描述"))
    if not title_prop or not time_prop or not weight_prop:
        return False, "体重表缺少标题、时间或数值字段"
    props = {
        title_prop: {"title": [{"text": {"content": f"{item.weight}kg"}}]},
        time_prop: {"date": {"start": _notion_datetime(item.time)}},
        weight_prop: {"number": item.weight},
    }
    if note_prop:
        props[note_prop] = {"rich_text": [{"text": {"content": item.note or ""}}]}
    return _notion_sync_page(db_id, props)


def _notion_sync_goal(goal: Dict[str, Any]) -> tuple[bool, str | None]:
    db_id = _resolve_notion_database_id(NOTION_GOALS_DB, "目标设置")
    if not db_id:
        return False, "未配置目标表"
    try:
        schema = _notion_database_schema(db_id)
    except Exception as exc:
        return False, f"读取目标表结构失败: {_notion_readable_error(exc, db_id, 'NOTION_GOALS_DB')}"
    title = f"{goal.get('start_date') or '-'} ~ {goal.get('end_date') or '-'}"
    title_prop = _notion_pick_property(schema, "title", ("周期", "目标", "计划"))
    start_prop = _notion_pick_property(schema, "date", ("开始", "起始"))
    end_prop = _notion_pick_property(schema, "date", ("结束", "截止"))
    start_weight_prop = _notion_pick_property(schema, "number", ("初始", "起始", "开始"))
    target_weight_prop = _notion_pick_property(schema, "number", ("目标",))
    current_weight_prop = _notion_pick_property(schema, "number", ("当前",))
    progress_prop = _notion_pick_property(schema, "number", ("进度",))
    pace_prop = _notion_pick_property(schema, "select", ("节奏", "状态"))
    if not title_prop or not start_weight_prop or not target_weight_prop:
        return False, "目标表缺少标题、初始体重或目标体重字段"
    props = {
        title_prop: {"title": [{"text": {"content": title}}]},
        start_weight_prop: {"number": goal.get("start_weight")},
        target_weight_prop: {"number": goal.get("target_weight")},
    }
    if start_prop and goal.get("start_date"):
        props[start_prop] = {"date": {"start": goal.get("start_date")}}
    if end_prop and goal.get("end_date"):
        props[end_prop] = {"date": {"start": goal.get("end_date")}}
    if current_weight_prop and goal.get("current_weight") is not None:
        props[current_weight_prop] = {"number": goal.get("current_weight")}
    if progress_prop and goal.get("progress_percent") is not None:
        props[progress_prop] = {"number": goal.get("progress_percent")}
    if pace_prop:
        props[pace_prop] = {"select": {"name": _goal_pace_label(goal.get("pace_status"))}}
    return _notion_sync_page(db_id, props)


def _notion_sync_sleep(item: SleepRecord) -> tuple[bool, str | None]:
    db_id = _resolve_notion_database_id(NOTION_SLEEP_DB, "睡眠记录")
    if not db_id:
        return False, "未配置睡眠表"
    try:
        schema = _notion_database_schema(db_id)
    except Exception as exc:
        return False, f"读取睡眠表结构失败: {_notion_readable_error(exc, db_id, 'NOTION_SLEEP_DB')}"
    title_prop = _notion_pick_property(schema, "title", ("睡眠", "记录", "时长"))
    time_prop = _notion_pick_property(schema, "date", ("时间", "日期"))
    hours_prop = _notion_pick_property(schema, "number", ("时长", "小时"))
    note_prop = _notion_pick_property(schema, "rich_text", ("备注", "说明", "描述"))
    if not title_prop or not time_prop or not hours_prop:
        return False, "睡眠表缺少标题、时间或时长字段"
    props = {
        title_prop: {"title": [{"text": {"content": f"{item.hours}h"}}]},
        time_prop: {"date": {"start": _notion_datetime(item.time)}},
        hours_prop: {"number": item.hours},
    }
    if note_prop:
        props[note_prop] = {"rich_text": [{"text": {"content": item.note or ""}}]}
    return _notion_sync_page(db_id, props)


def _notion_sync_exercise(item: ExerciseRecord) -> tuple[bool, str | None]:
    db_id = _resolve_notion_database_id(NOTION_EXERCISE_DB, "运动记录")
    if not db_id:
        return False, "未配置运动表"
    try:
        schema = _notion_database_schema(db_id)
    except Exception as exc:
        return False, f"读取运动表结构失败: {_notion_readable_error(exc, db_id, 'NOTION_EXERCISE_DB')}"
    title_prop = _notion_pick_property(schema, "title", ("运动", "记录", "时长"))
    time_prop = _notion_pick_property(schema, "date", ("时间", "日期"))
    minutes_prop = _notion_pick_property(schema, "number", ("时长", "分钟"))
    kind_prop = _notion_pick_property(schema, "rich_text", ("类型", "种类", "分类"))
    note_prop = _notion_pick_property(schema, "rich_text", ("备注", "说明", "描述"))
    if not title_prop or not time_prop or not minutes_prop:
        return False, "运动表缺少标题、时间或分钟字段"
    title = item.kind or "运动"
    props = {
        title_prop: {"title": [{"text": {"content": title}}]},
        time_prop: {"date": {"start": _notion_datetime(item.time)}},
        minutes_prop: {"number": item.minutes},
    }
    if kind_prop:
        props[kind_prop] = {"rich_text": [{"text": {"content": item.kind or ""}}]}
    if note_prop:
        props[note_prop] = {"rich_text": [{"text": {"content": item.note or ""}}]}
    return _notion_sync_page(db_id, props)


def _goal_pace_label(value: str) -> str:
    if value in ("reached",):
        return "已达标"
    if value in ("behind", "missed"):
        return "需调整"
    return "周期内有望达标"


def _coach_message(today: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "focus": str(today.get("coach_focus") or "执行重点"),
        "message": str(today.get("coach_message") or "保持节奏，优先完成今天的执行。"),
        "status_label": str(today.get("coach_status") or "待跟进"),
        "status_tone": str(today.get("coach_tone") or "neutral"),
        "flags": list(today.get("coach_flags") or []),
        "alerts": list(today.get("coach_alerts") or []),
    }


def _notion_latest_goal_snapshot() -> Dict[str, Any] | None:
    db_id = _resolve_notion_database_id(NOTION_GOALS_DB, "目标设置")
    if not (NOTION_TOKEN and db_id):
        return None
    try:
        schema = _notion_database_schema(db_id)
        start_prop = _notion_pick_property(schema, "date", ("开始", "起始"))
        end_prop = _notion_pick_property(schema, "date", ("结束", "截止"))
        start_weight_prop = _notion_pick_property(schema, "number", ("初始", "起始", "开始"))
        target_weight_prop = _notion_pick_property(schema, "number", ("目标",))
        current_weight_prop = _notion_pick_property(schema, "number", ("当前",))
        progress_prop = _notion_pick_property(schema, "number", ("进度",))
        pace_prop = _notion_pick_property(schema, "select", ("节奏", "状态"))
        title_prop = _notion_pick_property(schema, "title", ("周期", "目标", "计划"))
        payload = _notion_request_json(
            f"/databases/{db_id}/query",
            payload={
                "page_size": 1,
                "sorts": [{"timestamp": "last_edited_time", "direction": "descending"}],
            },
            method="POST",
        )
        results = payload.get("results") or []
        if not results:
            return None
        props = (results[0] or {}).get("properties") or {}
        goal: Dict[str, Any] = {
            "start_date": _notion_read_property(props.get(start_prop)) if start_prop else None,
            "end_date": _notion_read_property(props.get(end_prop)) if end_prop else None,
            "start_weight": _notion_read_property(props.get(start_weight_prop)) if start_weight_prop else None,
            "target_weight": _notion_read_property(props.get(target_weight_prop)) if target_weight_prop else None,
            "current_weight": _notion_read_property(props.get(current_weight_prop)) if current_weight_prop else None,
            "progress_percent": _notion_read_property(props.get(progress_prop)) if progress_prop else None,
            "pace_status": _notion_read_property(props.get(pace_prop)) if pace_prop else None,
        }
        title = _notion_read_property(props.get(title_prop)) if title_prop else None
        if isinstance(title, str):
            match = re.search(r"(?P<start>\d{4}-\d{2}-\d{2})\s*~\s*(?P<end>\d{4}-\d{2}-\d{2})", title)
            if match:
                goal.setdefault("start_date", match.group("start"))
                goal.setdefault("end_date", match.group("end"))
        if not any(goal.get(key) is not None for key in ("start_date", "end_date", "start_weight", "target_weight")):
            return None
        return goal
    except Exception as exc:
        print(f"警告: 读取 Notion 目标失败: {exc}")
        return None


def _hydrate_goal_from_notion(data: Dict[str, Any]) -> tuple[Dict[str, Any], str]:
    goal = dict(data.get("goal") or {})
    if goal.get("start_date") and goal.get("start_weight") and goal.get("target_weight"):
        return data, "local"

    remote = _notion_latest_goal_snapshot()
    if not remote:
        return data, "empty" if not goal else "local"

    merged = dict(goal)
    for key, value in remote.items():
        if value is not None and value != "":
            merged[key] = value
    data["goal"] = merged
    try:
        save_data(data)
    except Exception:
        pass
    return data, "notion"


class TrackerHandler(BaseHTTPRequestHandler):
    def _query_token(self) -> str:
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        return str(query.get("auth_token", [""])[0] or "").strip()

    def _is_request_allowed(self, body: Dict[str, Any] | None = None) -> bool:
        if not AUTH_TOKEN:
            return True
        header = self.headers.get("X-Auth-Token", "").strip()
        if header == AUTH_TOKEN:
            return True
        if self._query_token() == AUTH_TOKEN:
            return True
        token = str((body or {}).get("auth_token", "")).strip()
        return token == AUTH_TOKEN

    def _json_response(self, payload: Dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _text_response(self, text: str, status: HTTPStatus = HTTPStatus.OK, content_type: str = "text/plain; charset=utf-8") -> None:
        raw = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _read_json_body(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    def _serve_file(self, file_path: Path) -> None:
        if not file_path.exists() or not file_path.is_file():
            self._text_response("Not Found", HTTPStatus.NOT_FOUND)
            return

        ext = file_path.suffix.lower()
        content_type = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".json": "application/json; charset=utf-8",
            ".ico": "image/x-icon",
        }.get(ext, "application/octet-stream")

        data = file_path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _status_payload(self) -> Dict[str, Any]:
        data = load_data()
        data, goal_source = _hydrate_goal_from_notion(data)
        plan = data.get("plan", DEFAULT_PLAN)
        active = data.get("active_fast")
        records = data.get("records", [])
        meals = data.get("meals", [])
        weights = data.get("weight_logs", [])
        sleeps = data.get("sleep_logs", [])
        exercises = data.get("exercise_logs", [])

        elapsed_hours = 0.0
        remaining_hours = 16.0
        if active:
            start = parse_time(active)
            elapsed_hours = hours_between(start, now_local())
            remaining_hours = max(0.0, round(16.0 - elapsed_hours, 2))

        success_count = sum(1 for r in records if r.get("success"))
        today = now_local().strftime(DATE_FMT)
        today_status = evaluate_day(today, data)
        week = period_stats(data, 7)
        month = period_stats(data, 30)
        goal = goal_stats(data)

        coach = _coach_message(today_status)
        return {
            "ok": True,
            "plan": plan,
            "goal": goal,
            "goal_source": goal_source,
            "active_fast": active,
            "elapsed_hours": elapsed_hours,
            "remaining_hours": remaining_hours,
            "total_days": len(records),
            "success_days": success_count,
            "streak_days": calc_streak(records),
            "records": sorted(records, key=lambda r: r["date"], reverse=True),
            "meals": sorted(meals, key=lambda m: m["time"], reverse=True),
            "weights": sorted(weights, key=lambda w: w["time"], reverse=True),
            "sleeps": sorted(sleeps, key=lambda s: s["time"], reverse=True),
            "exercises": sorted(exercises, key=lambda e: e["time"], reverse=True),
            "today": today_status,
            "coach": coach,
            "week_stats": week,
            "month_stats": month,
            "weight_7": weight_stats(data, 7),
            "weight_30": weight_stats(data, 30),
        }

    def do_GET(self) -> None:  # noqa: N802
        route = urlparse(self.path).path
        if route in ("/", "/index.html"):
            return self._serve_file(WEB_DIR / "index.html")
        if route == "/styles.css":
            return self._serve_file(WEB_DIR / "styles.css")
        if route == "/app.js":
            return self._serve_file(WEB_DIR / "app.js")
        if route == "/api/status":
            if not self._is_request_allowed():
                return self._json_response({"ok": False, "error": "未授权", "need_auth": True}, HTTPStatus.UNAUTHORIZED)
            return self._json_response(self._status_payload())

        return self._text_response("Not Found", HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802
        route = urlparse(self.path).path
        try:
            body = self._read_json_body()
        except json.JSONDecodeError:
            return self._json_response({"ok": False, "error": "请求体不是有效 JSON"}, HTTPStatus.BAD_REQUEST)
        if not self._is_request_allowed(body):
            return self._json_response({"ok": False, "error": "未授权", "need_auth": True}, HTTPStatus.UNAUTHORIZED)

        if route == "/api/start":
            return self._handle_start(body)
        if route == "/api/end":
            return self._handle_end(body)
        if route == "/api/checkin":
            return self._handle_checkin(body)
        if route == "/api/meal":
            return self._handle_meal(body)
        if route == "/api/meal/update":
            return self._handle_meal_update(body)
        if route == "/api/weight":
            return self._handle_weight(body)
        if route == "/api/sleep":
            return self._handle_sleep(body)
        if route == "/api/exercise":
            return self._handle_exercise(body)
        if route == "/api/window":
            return self._handle_window(body)
        if route == "/api/goal":
            return self._handle_goal(body)
        if route == "/api/reset":
            return self._handle_reset(body)

        return self._json_response({"ok": False, "error": "接口不存在"}, HTTPStatus.NOT_FOUND)

    def _handle_start(self, body: Dict[str, Any]) -> None:
        data = load_data()
        if data.get("active_fast"):
            return self._json_response(
                {"ok": False, "error": f"你已经在断食中，开始时间: {data['active_fast']}"},
                HTTPStatus.BAD_REQUEST,
            )

        start_at = body.get("time")
        start_time = parse_time(start_at) if start_at else now_local()
        data["active_fast"] = start_time.strftime(TIME_FMT)
        save_data(data)
        return self._json_response({"ok": True, "message": f"已开始断食: {data['active_fast']}"})

    def _handle_end(self, body: Dict[str, Any]) -> None:
        data = load_data()
        if not data.get("active_fast"):
            return self._json_response({"ok": False, "error": "当前没有进行中的断食"}, HTTPStatus.BAD_REQUEST)

        target = float(body.get("target", 16.0))
        note = str(body.get("note", "")).strip()
        end_at = body.get("time")

        start = parse_time(data["active_fast"])
        end_time = parse_time(end_at) if end_at else now_local()
        if end_time <= start:
            return self._json_response({"ok": False, "error": "结束时间必须晚于开始时间"}, HTTPStatus.BAD_REQUEST)

        duration = hours_between(start, end_time)
        success = duration >= target

        record = Record(
            date=end_time.strftime(DATE_FMT),
            start=start.strftime(TIME_FMT),
            end=end_time.strftime(TIME_FMT),
            hours=duration,
            success=success,
            note=note,
        )
        data["records"].append(asdict(record))
        data["active_fast"] = None
        save_data(data)

        status = evaluate_day(record.date, data)
        return self._json_response(
            {
                "ok": True,
                "message": f"本次断食 {duration} 小时，目标 {target} 小时，结果: {'达标' if success else '未达标'}",
                "success": success,
                "duration": duration,
                "day_status": status,
            }
        )

    def _handle_checkin(self, body: Dict[str, Any]) -> None:
        data = load_data()
        day = str(body.get("date", now_local().strftime(DATE_FMT)))
        note = str(body.get("note", "手动打卡"))

        for r in data["records"]:
            if r["date"] == day:
                return self._json_response({"ok": False, "error": f"{day} 已存在记录"}, HTTPStatus.BAD_REQUEST)

        data["records"].append(
            asdict(
                Record(
                    date=day,
                    start=f"{day} 00:00",
                    end=f"{day} 16:00",
                    hours=16.0,
                    success=True,
                    note=note,
                )
            )
        )
        save_data(data)
        return self._json_response({"ok": True, "message": f"已完成 {day} 手动打卡"})

    def _handle_meal(self, body: Dict[str, Any]) -> None:
        data = load_data()

        food = str(body.get("food", "")).strip()
        if not food:
            return self._json_response({"ok": False, "error": "food 不能为空"}, HTTPStatus.BAD_REQUEST)

        raw_time = str(body.get("time", "")).strip()
        meal_amount = str(body.get("meal_amount", "正常")).strip() or "正常"
        diet_types = _coerce_text_list(body.get("diet_types"))
        risk_scenarios = _coerce_text_list(body.get("risk_scenarios"))
        note = str(body.get("note", "")).strip()
        meal_dt = parse_time(raw_time) if raw_time else now_local()

        meal = MealRecord(
            date=meal_dt.strftime(DATE_FMT),
            time=meal_dt.strftime(TIME_FMT),
            food=food,
            meal_amount=meal_amount,
            diet_types=diet_types,
            risk_scenarios=risk_scenarios,
            note=note,
        )
        meal_dict = asdict(meal)
        meal_dict["id"] = str(body.get("meal_id", "")).strip() or f"meal-{meal_dt.strftime('%Y%m%d%H%M')}-{len(data.setdefault('meals', [])) + 1}"
        data.setdefault("meals", []).append(meal_dict)
        save_data(data)

        in_window = is_meal_in_window(meal.time, data.get("plan", DEFAULT_PLAN))
        cloud_synced = None
        cloud_error = None
        if _notion_enabled():
            cloud_synced, cloud_error = _notion_sync_meal(meal, in_window, prefer_update=True)
        return self._json_response(
            {
                "ok": True,
                "message": f"已记录进食: {meal.time} | {meal.food}",
                "in_window": in_window,
                "cloud_synced": cloud_synced,
                "cloud_error": cloud_error,
            }
        )

    def _handle_meal_update(self, body: Dict[str, Any]) -> None:
        data = load_data()
        meals = data.setdefault("meals", [])
        if not meals:
            return self._json_response({"ok": False, "error": "暂无进食记录可补标签"}, HTTPStatus.BAD_REQUEST)

        meal_id = str(body.get("meal_id", "")).strip()
        meal_time = str(body.get("time", "")).strip()
        meal_food = str(body.get("food", "")).strip()
        target_index = -1

        if meal_id:
            for idx in range(len(meals) - 1, -1, -1):
                if str(meals[idx].get("id", "")).strip() == meal_id:
                    target_index = idx
                    break
        if target_index < 0 and meal_time and meal_food:
            for idx in range(len(meals) - 1, -1, -1):
                item = meals[idx]
                if str(item.get("time", "")).strip() == meal_time and str(item.get("food", "")).strip() == meal_food:
                    target_index = idx
                    break
        if target_index < 0 and meal_time:
            for idx in range(len(meals) - 1, -1, -1):
                if str(meals[idx].get("time", "")).strip() == meal_time:
                    target_index = idx
                    break
        if target_index < 0:
            return self._json_response({"ok": False, "error": "未找到待补标签的记录"}, HTTPStatus.BAD_REQUEST)

        target = dict(meals[target_index])
        if "meal_amount" in body:
            target["meal_amount"] = str(body.get("meal_amount", "正常")).strip() or "正常"
        if "diet_types" in body:
            target["diet_types"] = _coerce_text_list(body.get("diet_types"))
        if "risk_scenarios" in body:
            target["risk_scenarios"] = _coerce_text_list(body.get("risk_scenarios"))
        if "note" in body:
            target["note"] = str(body.get("note", "")).strip()

        meals[target_index] = target
        save_data(data)

        in_window = is_meal_in_window(str(target.get("time", "")), data.get("plan", DEFAULT_PLAN))
        cloud_synced = None
        cloud_error = None
        if _notion_enabled():
            meal = MealRecord(
                date=str(target.get("date", "")),
                time=str(target.get("time", "")),
                food=str(target.get("food", "")),
                meal_amount=str(target.get("meal_amount", "正常")),
                diet_types=_coerce_text_list(target.get("diet_types")),
                risk_scenarios=_coerce_text_list(target.get("risk_scenarios")),
                note=str(target.get("note", "")),
            )
            cloud_synced, cloud_error = _notion_sync_meal(meal, in_window, prefer_update=True)

        return self._json_response(
            {
                "ok": True,
                "message": f"已补充标签: {target.get('time', '')} | {target.get('food', '')}",
                "cloud_synced": cloud_synced,
                "cloud_error": cloud_error,
                "in_window": in_window,
            }
        )

    def _handle_weight(self, body: Dict[str, Any]) -> None:
        data = load_data()

        try:
            value = float(body.get("value", 0))
        except (TypeError, ValueError):
            return self._json_response({"ok": False, "error": "体重数值无效"}, HTTPStatus.BAD_REQUEST)

        if value <= 0:
            return self._json_response({"ok": False, "error": "体重必须大于0"}, HTTPStatus.BAD_REQUEST)

        raw_time = str(body.get("time", "")).strip()
        note = str(body.get("note", "")).strip()
        dt = parse_time(raw_time) if raw_time else now_local()

        item = WeightRecord(
            date=dt.strftime(DATE_FMT),
            time=dt.strftime(TIME_FMT),
            weight=round(value, 2),
            note=note,
        )
        data.setdefault("weight_logs", []).append(asdict(item))
        save_data(data)

        cloud_synced = None
        cloud_error = None
        if _notion_enabled():
            cloud_synced, cloud_error = _notion_sync_weight(item)
        return self._json_response(
            {
                "ok": True,
                "message": f"已记录体重: {item.time} | {item.weight} kg",
                "cloud_synced": cloud_synced,
                "cloud_error": cloud_error,
            }
        )

    def _handle_sleep(self, body: Dict[str, Any]) -> None:
        data = load_data()
        try:
            hours = float(body.get("hours", 0))
        except (TypeError, ValueError):
            return self._json_response({"ok": False, "error": "睡眠时长无效"}, HTTPStatus.BAD_REQUEST)
        if hours <= 0 or hours > 24:
            return self._json_response({"ok": False, "error": "睡眠时长需在 0-24"}, HTTPStatus.BAD_REQUEST)

        raw_time = str(body.get("time", "")).strip()
        note = str(body.get("note", "")).strip()
        dt = parse_time(raw_time) if raw_time else now_local()

        item = SleepRecord(
            date=dt.strftime(DATE_FMT),
            time=dt.strftime(TIME_FMT),
            hours=round(hours, 1),
            note=note,
        )
        data.setdefault("sleep_logs", []).append(asdict(item))
        save_data(data)
        cloud_synced = None
        cloud_error = None
        if _notion_enabled():
            cloud_synced, cloud_error = _notion_sync_sleep(item)
        return self._json_response(
            {
                "ok": True,
                "message": f"已记录睡眠: {item.time} | {item.hours} 小时",
                "cloud_synced": cloud_synced,
                "cloud_error": cloud_error,
            }
        )

    def _handle_exercise(self, body: Dict[str, Any]) -> None:
        data = load_data()
        try:
            minutes = int(body.get("minutes", 0))
        except (TypeError, ValueError):
            return self._json_response({"ok": False, "error": "运动时长无效"}, HTTPStatus.BAD_REQUEST)
        if minutes <= 0 or minutes > 480:
            return self._json_response({"ok": False, "error": "运动时长需在 1-480"}, HTTPStatus.BAD_REQUEST)

        raw_time = str(body.get("time", "")).strip()
        kind = str(body.get("kind", "")).strip()
        note = str(body.get("note", "")).strip()
        dt = parse_time(raw_time) if raw_time else now_local()

        item = ExerciseRecord(
            date=dt.strftime(DATE_FMT),
            time=dt.strftime(TIME_FMT),
            minutes=minutes,
            kind=kind,
            note=note,
        )
        data.setdefault("exercise_logs", []).append(asdict(item))
        save_data(data)
        cloud_synced = None
        cloud_error = None
        if _notion_enabled():
            cloud_synced, cloud_error = _notion_sync_exercise(item)
        label = kind or "运动"
        return self._json_response(
            {
                "ok": True,
                "message": f"已记录运动: {item.time} | {label} {item.minutes} 分钟",
                "cloud_synced": cloud_synced,
                "cloud_error": cloud_error,
            }
        )

    def _handle_window(self, body: Dict[str, Any]) -> None:
        data = load_data()

        start = str(body.get("start", "")).strip()
        if not start:
            return self._json_response({"ok": False, "error": "start 不能为空"}, HTTPStatus.BAD_REQUEST)

        try:
            parse_clock(start)
        except ValueError as exc:
            return self._json_response({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)

        try:
            hours = int(body.get("hours", 8))
        except (TypeError, ValueError):
            return self._json_response({"ok": False, "error": "hours 无效"}, HTTPStatus.BAD_REQUEST)

        if hours <= 0 or hours > 24:
            return self._json_response({"ok": False, "error": "hours 必须在 1-24"}, HTTPStatus.BAD_REQUEST)

        data["plan"] = {"start": start, "hours": hours}
        save_data(data)
        return self._json_response({"ok": True, "message": f"已设置进食窗口: {start} + {hours}小时"})

    def _handle_goal(self, body: Dict[str, Any]) -> None:
        data = load_data()

        start_date = str(body.get("start_date", "")).strip()
        if not start_date:
            return self._json_response({"ok": False, "error": "start_date 不能为空"}, HTTPStatus.BAD_REQUEST)
        try:
            start_day = datetime.strptime(start_date, DATE_FMT).date()
        except ValueError:
            return self._json_response({"ok": False, "error": "start_date 格式应为 YYYY-MM-DD"}, HTTPStatus.BAD_REQUEST)

        end_date = str(body.get("end_date", "")).strip()
        if not end_date:
            end_date = (start_day + timedelta(days=61)).strftime(DATE_FMT)
        try:
            end_day = datetime.strptime(end_date, DATE_FMT).date()
        except ValueError:
            return self._json_response({"ok": False, "error": "end_date 格式应为 YYYY-MM-DD"}, HTTPStatus.BAD_REQUEST)
        if end_day < start_day:
            return self._json_response({"ok": False, "error": "end_date 不能早于 start_date"}, HTTPStatus.BAD_REQUEST)

        try:
            start_weight = float(body.get("start_weight", 0))
            target_weight = float(body.get("target_weight", 0))
        except (TypeError, ValueError):
            return self._json_response({"ok": False, "error": "体重参数无效"}, HTTPStatus.BAD_REQUEST)

        if start_weight <= 0 or target_weight <= 0:
            return self._json_response({"ok": False, "error": "体重必须大于0"}, HTTPStatus.BAD_REQUEST)

        data["goal"] = {
            "start_date": start_date,
            "end_date": end_date,
            "start_weight": round(start_weight, 2),
            "target_weight": round(target_weight, 2),
        }
        save_data(data)
        cloud_synced = None
        cloud_error = None
        if _notion_enabled():
            cloud_synced, cloud_error = _notion_sync_goal(goal_stats(data))
        return self._json_response(
            {
                "ok": True,
                "message": "已保存减脂目标",
                "cloud_synced": cloud_synced,
                "cloud_error": cloud_error,
            }
        )

    def _handle_reset(self, body: Dict[str, Any]) -> None:
        scope = str(body.get("scope", "local")).strip().lower()
        if scope not in {"local", "all"}:
            scope = "local"

        archived_total = 0
        cloud_error = None
        if scope == "all":
            if not _notion_enabled():
                return self._json_response(
                    {
                        "ok": False,
                        "error": "未配置 Notion，无法清空云端",
                        "cloud_synced": False,
                        "cloud_error": "未配置 Notion",
                    },
                )
            resolved = _resolve_reset_database_ids()
            for title, db_id in resolved.items():
                try:
                    archived, error = _notion_archive_database(db_id)
                    archived_total += archived
                    if error:
                        cloud_error = f"{title}: {error}"
                        break
                except Exception as exc:
                    cloud_error = f"{title}: {_notion_readable_error(exc, db_id, '数据库')}"
                    break
            if cloud_error:
                return self._json_response(
                    {
                        "ok": False,
                        "error": f"初始化完成，但云端清理失败：{cloud_error}",
                        "cloud_synced": False,
                        "cloud_error": cloud_error,
                    }
                )

        _reset_local_data()

        message = "已初始化本地数据"
        if scope == "all":
            message = "已初始化本地与云端测试数据"

        return self._json_response(
            {
                "ok": True,
                "message": message,
                "cloud_synced": scope != "all" or cloud_error is None,
                "cloud_error": cloud_error,
                "archived_total": archived_total,
                "scope": scope,
            }
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="8+16 手机打卡服务")
    default_host = os.getenv("HOST", "0.0.0.0")
    default_port = int(os.getenv("PORT", "8000"))
    parser.add_argument("--host", default=default_host, help="监听地址，默认 0.0.0.0")
    parser.add_argument("--port", type=int, default=default_port, help="端口，默认 8000 或环境变量 PORT")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    server = ThreadingHTTPServer((args.host, args.port), TrackerHandler)
    print(f"服务已启动: http://{args.host}:{args.port}")
    print("手机访问时请把 host 替换为电脑局域网 IP，例如: http://192.168.1.8:8000")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止")


if __name__ == "__main__":
    main()
