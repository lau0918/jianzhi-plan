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
    if database_id in _NOTION_DB_SCHEMA_CACHE:
        return _NOTION_DB_SCHEMA_CACHE[database_id]
    schema = _notion_request_json(f"/databases/{database_id}", method="GET")
    _NOTION_DB_SCHEMA_CACHE[database_id] = schema
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
    if kind == "checkbox":
        return prop.get("checkbox")
    return None


def _notion_sync_page(database_id: str, properties: Dict[str, Any]) -> tuple[bool, str | None]:
    if not NOTION_TOKEN or not database_id:
        return False, "未配置 Notion"
    payload = {"parent": {"database_id": database_id}, "properties": properties}
    try:
        _notion_request_json("/pages", payload=payload, method="POST")
        return True, None
    except Exception as exc:
        message = str(exc)
        print(f"警告: Notion 写入失败: {message}")
        return False, message


def _notion_sync_meal(meal: MealRecord, in_window: bool) -> tuple[bool, str | None]:
    if not NOTION_MEALS_DB:
        return False, "未配置进食表"
    try:
        schema = _notion_database_schema(NOTION_MEALS_DB)
    except Exception as exc:
        return False, f"读取进食表结构失败: {exc}"
    title_prop = _notion_pick_property(schema, "title", ("食物", "名称", "记录"))
    time_prop = _notion_pick_property(schema, "date", ("时间", "日期"))
    window_prop = _notion_pick_property(schema, "select", ("窗口状态", "状态", "类型", "分类"))
    note_prop = _notion_pick_property(schema, "rich_text", ("备注", "说明", "描述"))
    if not title_prop or not time_prop:
        return False, "进食表缺少标题或时间字段"
    props = {
        title_prop: {"title": [{"text": {"content": meal.food}}]},
        time_prop: {"date": {"start": _notion_datetime(meal.time)}},
    }
    if window_prop:
        props[window_prop] = {"select": {"name": "窗口内" if in_window else "窗口外"}}
    if note_prop:
        props[note_prop] = {"rich_text": [{"text": {"content": meal.note or ""}}]}
    return _notion_sync_page(NOTION_MEALS_DB, props)


def _notion_sync_weight(item: WeightRecord) -> tuple[bool, str | None]:
    if not NOTION_WEIGHTS_DB:
        return False, "未配置体重表"
    try:
        schema = _notion_database_schema(NOTION_WEIGHTS_DB)
    except Exception as exc:
        return False, f"读取体重表结构失败: {exc}"
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
    return _notion_sync_page(NOTION_WEIGHTS_DB, props)


def _notion_sync_goal(goal: Dict[str, Any]) -> tuple[bool, str | None]:
    if not NOTION_GOALS_DB:
        return False, "未配置目标表"
    try:
        schema = _notion_database_schema(NOTION_GOALS_DB)
    except Exception as exc:
        return False, f"读取目标表结构失败: {exc}"
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
    return _notion_sync_page(NOTION_GOALS_DB, props)


def _notion_sync_sleep(item: SleepRecord) -> tuple[bool, str | None]:
    if not NOTION_SLEEP_DB:
        return False, "未配置睡眠表"
    try:
        schema = _notion_database_schema(NOTION_SLEEP_DB)
    except Exception as exc:
        return False, f"读取睡眠表结构失败: {exc}"
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
    return _notion_sync_page(NOTION_SLEEP_DB, props)


def _notion_sync_exercise(item: ExerciseRecord) -> tuple[bool, str | None]:
    if not NOTION_EXERCISE_DB:
        return False, "未配置运动表"
    try:
        schema = _notion_database_schema(NOTION_EXERCISE_DB)
    except Exception as exc:
        return False, f"读取运动表结构失败: {exc}"
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
    return _notion_sync_page(NOTION_EXERCISE_DB, props)


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
    if not (NOTION_TOKEN and NOTION_GOALS_DB):
        return None
    try:
        schema = _notion_database_schema(NOTION_GOALS_DB)
        start_prop = _notion_pick_property(schema, "date", ("开始", "起始"))
        end_prop = _notion_pick_property(schema, "date", ("结束", "截止"))
        start_weight_prop = _notion_pick_property(schema, "number", ("初始", "起始", "开始"))
        target_weight_prop = _notion_pick_property(schema, "number", ("目标",))
        current_weight_prop = _notion_pick_property(schema, "number", ("当前",))
        progress_prop = _notion_pick_property(schema, "number", ("进度",))
        pace_prop = _notion_pick_property(schema, "select", ("节奏", "状态"))
        title_prop = _notion_pick_property(schema, "title", ("周期", "目标", "计划"))
        payload = _notion_request_json(
            f"/databases/{NOTION_GOALS_DB}/query",
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
        note = str(body.get("note", "")).strip()
        meal_dt = parse_time(raw_time) if raw_time else now_local()

        meal = MealRecord(
            date=meal_dt.strftime(DATE_FMT),
            time=meal_dt.strftime(TIME_FMT),
            food=food,
            note=note,
        )
        data.setdefault("meals", []).append(asdict(meal))
        save_data(data)

        in_window = is_meal_in_window(meal.time, data.get("plan", DEFAULT_PLAN))
        cloud_synced = None
        cloud_error = None
        if _notion_enabled():
            cloud_synced, cloud_error = _notion_sync_meal(meal, in_window)
        return self._json_response(
            {
                "ok": True,
                "message": f"已记录进食: {meal.time} | {meal.food}",
                "in_window": in_window,
                "cloud_synced": cloud_synced,
                "cloud_error": cloud_error,
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
