#!/usr/bin/env python3
"""Mobile-friendly web server for 8+16 fasting tracker."""

from __future__ import annotations

import argparse
import json
import os
import urllib.request
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


def _notion_create_page(database_id: str, properties: Dict[str, Any]) -> None:
    if not NOTION_TOKEN or not database_id:
        return
    payload = {"parent": {"database_id": database_id}, "properties": properties}
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        "https://api.notion.com/v1/pages",
        data=data,
        headers={
            "Authorization": f"Bearer {NOTION_TOKEN}",
            "Content-Type": "application/json",
            "Notion-Version": "2022-06-28",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=6) as resp:
            resp.read()
    except Exception as exc:
        print(f"警告: Notion 写入失败: {exc}")


def _notion_sync_meal(meal: MealRecord, in_window: bool) -> None:
    if not NOTION_MEALS_DB:
        return
    props = {
        "食物": {"title": [{"text": {"content": meal.food}}]},
        "时间": {"date": {"start": _notion_datetime(meal.time)}},
        "窗口状态": {"select": {"name": "窗口内" if in_window else "窗口外"}},
        "备注": {"rich_text": [{"text": {"content": meal.note or ""}}]},
    }
    _notion_create_page(NOTION_MEALS_DB, props)


def _notion_sync_weight(item: WeightRecord) -> None:
    if not NOTION_WEIGHTS_DB:
        return
    props = {
        "体重记录": {"title": [{"text": {"content": f"{item.weight}kg"}}]},
        "时间": {"date": {"start": _notion_datetime(item.time)}},
        "体重(kg)": {"number": item.weight},
        "备注": {"rich_text": [{"text": {"content": item.note or ""}}]},
    }
    _notion_create_page(NOTION_WEIGHTS_DB, props)


def _notion_sync_goal(goal: Dict[str, Any]) -> None:
    if not NOTION_GOALS_DB:
        return
    title = f"{goal.get('start_date') or '-'} ~ {goal.get('end_date') or '-'}"
    props = {
        "周期": {"title": [{"text": {"content": title}}]},
        "开始日期": {"date": {"start": goal.get("start_date")}} if goal.get("start_date") else None,
        "结束日期": {"date": {"start": goal.get("end_date")}} if goal.get("end_date") else None,
        "初始体重": {"number": goal.get("start_weight")},
        "目标体重": {"number": goal.get("target_weight")},
        "当前体重": {"number": goal.get("current_weight")},
        "目标进度(%)": {"number": goal.get("progress_percent")},
        "节奏判定": {"select": {"name": _goal_pace_label(goal.get("pace_status"))}},
    }
    clean_props = {k: v for k, v in props.items() if v is not None}
    _notion_create_page(NOTION_GOALS_DB, clean_props)


def _notion_sync_sleep(item: SleepRecord) -> None:
    if not NOTION_SLEEP_DB:
        return
    props = {
        "睡眠记录": {"title": [{"text": {"content": f"{item.hours}h"}}]},
        "时间": {"date": {"start": _notion_datetime(item.time)}},
        "时长(小时)": {"number": item.hours},
        "备注": {"rich_text": [{"text": {"content": item.note or ""}}]},
    }
    _notion_create_page(NOTION_SLEEP_DB, props)


def _notion_sync_exercise(item: ExerciseRecord) -> None:
    if not NOTION_EXERCISE_DB:
        return
    title = item.kind or "运动"
    props = {
        "运动记录": {"title": [{"text": {"content": title}}]},
        "时间": {"date": {"start": _notion_datetime(item.time)}},
        "时长(分钟)": {"number": item.minutes},
        "类型": {"rich_text": [{"text": {"content": item.kind or ""}}]},
        "备注": {"rich_text": [{"text": {"content": item.note or ""}}]},
    }
    _notion_create_page(NOTION_EXERCISE_DB, props)


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


class TrackerHandler(BaseHTTPRequestHandler):
    def _is_write_allowed(self, body: Dict[str, Any]) -> bool:
        if not AUTH_TOKEN:
            return True
        header = self.headers.get("X-Auth-Token", "").strip()
        if header == AUTH_TOKEN:
            return True
        token = str(body.get("auth_token", "")).strip()
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
        if self.path in ("/", "/index.html"):
            return self._serve_file(WEB_DIR / "index.html")
        if self.path == "/styles.css":
            return self._serve_file(WEB_DIR / "styles.css")
        if self.path == "/app.js":
            return self._serve_file(WEB_DIR / "app.js")
        if self.path == "/api/status":
            return self._json_response(self._status_payload())

        return self._text_response("Not Found", HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802
        try:
            body = self._read_json_body()
        except json.JSONDecodeError:
            return self._json_response({"ok": False, "error": "请求体不是有效 JSON"}, HTTPStatus.BAD_REQUEST)
        if not self._is_write_allowed(body):
            return self._json_response({"ok": False, "error": "未授权"}, HTTPStatus.UNAUTHORIZED)

        if self.path == "/api/start":
            return self._handle_start(body)
        if self.path == "/api/end":
            return self._handle_end(body)
        if self.path == "/api/checkin":
            return self._handle_checkin(body)
        if self.path == "/api/meal":
            return self._handle_meal(body)
        if self.path == "/api/weight":
            return self._handle_weight(body)
        if self.path == "/api/sleep":
            return self._handle_sleep(body)
        if self.path == "/api/exercise":
            return self._handle_exercise(body)
        if self.path == "/api/window":
            return self._handle_window(body)
        if self.path == "/api/goal":
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
        if _notion_enabled():
            _notion_sync_meal(meal, in_window)
        return self._json_response(
            {
                "ok": True,
                "message": f"已记录进食: {meal.time} | {meal.food}",
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

        if _notion_enabled():
            _notion_sync_weight(item)
        return self._json_response({"ok": True, "message": f"已记录体重: {item.time} | {item.weight} kg"})

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
        if _notion_enabled():
            _notion_sync_sleep(item)
        return self._json_response({"ok": True, "message": f"已记录睡眠: {item.time} | {item.hours} 小时"})

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
        if _notion_enabled():
            _notion_sync_exercise(item)
        label = kind or "运动"
        return self._json_response({"ok": True, "message": f"已记录运动: {item.time} | {label} {item.minutes} 分钟"})

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
        if _notion_enabled():
            _notion_sync_goal(goal_stats(data))
        return self._json_response({"ok": True, "message": "已保存减脂目标"})


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
