#!/usr/bin/env python3
"""8+16 fasting tracker with meal, weight, stats and excel export."""

from __future__ import annotations

import argparse
import json
import sys
import zipfile
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from xml.sax.saxutils import escape

DATA_FILE = Path("fasting_data.json")
EXCEL_FILE = Path("fasting_report.xlsx")
TIME_FMT = "%Y-%m-%d %H:%M"
DATE_FMT = "%Y-%m-%d"
CLOCK_FMT = "%H:%M"
DEFAULT_PLAN = {"start": "10:00", "hours": 8}
DEFAULT_GOAL = {"start_date": "", "end_date": "", "start_weight": None, "target_weight": None}


@dataclass
class Record:
    date: str
    start: str
    end: str
    hours: float
    success: bool
    note: str = ""


@dataclass
class MealRecord:
    date: str
    time: str
    food: str
    note: str = ""


@dataclass
class WeightRecord:
    date: str
    time: str
    weight: float = 0.0
    note: str = ""


@dataclass
class SleepRecord:
    date: str
    time: str
    hours: float = 0.0
    note: str = ""


@dataclass
class ExerciseRecord:
    date: str
    time: str
    minutes: int = 0
    kind: str = ""
    note: str = ""


@dataclass
class WaistRecord:
    date: str
    time: str
    cm: float = 0.0
    note: str = ""


def now_local() -> datetime:
    return datetime.now().replace(second=0, microsecond=0)


def parse_time(value: str) -> datetime:
    try:
        return datetime.strptime(value, TIME_FMT)
    except ValueError as exc:
        raise ValueError(f"时间格式错误: {value}，请使用: {TIME_FMT}") from exc


def parse_clock(value: str) -> time:
    try:
        return datetime.strptime(value, CLOCK_FMT).time()
    except ValueError as exc:
        raise ValueError(f"时间窗口格式错误: {value}，请使用: {CLOCK_FMT}") from exc


def load_data() -> Dict[str, Any]:
    if not DATA_FILE.exists():
        return {
            "active_fast": None,
            "records": [],
            "meals": [],
            "weight_logs": [],
            "sleep_logs": [],
            "exercise_logs": [],
            "waist_logs": [],
            "plan": dict(DEFAULT_PLAN),
            "goal": dict(DEFAULT_GOAL),
        }

    with DATA_FILE.open("r", encoding="utf-8") as f:
        data = json.load(f)

    data.setdefault("active_fast", None)
    data.setdefault("records", [])
    data.setdefault("meals", [])
    data.setdefault("weight_logs", [])
    data.setdefault("sleep_logs", [])
    data.setdefault("exercise_logs", [])
    data.setdefault("waist_logs", [])
    plan = data.get("plan") or {}
    data["plan"] = {
        "start": str(plan.get("start") or DEFAULT_PLAN["start"]),
        "hours": int(plan.get("hours") or DEFAULT_PLAN["hours"]),
    }
    goal = data.get("goal") or {}
    data["goal"] = {
        "start_date": str(goal.get("start_date") or DEFAULT_GOAL["start_date"]),
        "end_date": str(goal.get("end_date") or DEFAULT_GOAL["end_date"]),
        "start_weight": goal.get("start_weight"),
        "target_weight": goal.get("target_weight"),
    }
    return data


def _cell_ref(col_index: int, row_index: int) -> str:
    col = ""
    idx = col_index
    while idx > 0:
        idx, rem = divmod(idx - 1, 26)
        col = chr(65 + rem) + col
    return f"{col}{row_index}"


def _xml_cell(col_index: int, row_index: int, value: Any) -> str:
    ref = _cell_ref(col_index, row_index)
    if value is None:
        return f'<c r="{ref}" t="inlineStr"><is><t></t></is></c>'
    if isinstance(value, bool):
        text = "TRUE" if value else "FALSE"
        return f'<c r="{ref}" t="inlineStr"><is><t>{text}</t></is></c>'
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f'<c r="{ref}"><v>{value}</v></c>'
    text = escape(str(value))
    return f'<c r="{ref}" t="inlineStr"><is><t>{text}</t></is></c>'


def _sheet_xml(headers: List[str], rows: List[List[Any]]) -> str:
    all_rows = [headers] + rows
    row_xml_parts: List[str] = []
    for ridx, row in enumerate(all_rows, start=1):
        cells = "".join(_xml_cell(cidx, ridx, val) for cidx, val in enumerate(row, start=1))
        row_xml_parts.append(f'<row r="{ridx}">{cells}</row>')
    rows_xml = "".join(row_xml_parts)
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f"<sheetData>{rows_xml}</sheetData>"
        "</worksheet>"
    )


def window_bounds(for_day: str, plan: Dict[str, Any]) -> Tuple[datetime, datetime]:
    start_clock = parse_clock(str(plan.get("start", DEFAULT_PLAN["start"])))
    start_dt = datetime.combine(datetime.strptime(for_day, DATE_FMT).date(), start_clock)
    end_dt = start_dt + timedelta(hours=int(plan.get("hours", DEFAULT_PLAN["hours"])))
    return start_dt, end_dt


def is_meal_in_window(meal_time_str: str, plan: Dict[str, Any]) -> bool:
    meal_dt = parse_time(meal_time_str)
    start_dt, end_dt = window_bounds(meal_dt.strftime(DATE_FMT), plan)
    return start_dt <= meal_dt <= end_dt


def hours_between(start: datetime, end: datetime) -> float:
    return round((end - start).total_seconds() / 3600, 2)


def calc_streak(records: List[Dict[str, Any]]) -> int:
    success_dates = sorted(
        {datetime.strptime(r["date"], DATE_FMT).date() for r in records if r.get("success")},
        reverse=True,
    )
    if not success_dates:
        return 0

    streak = 0
    expected = datetime.now().date()
    if success_dates[0] != expected:
        expected = expected - timedelta(days=1)

    for d in success_dates:
        if d == expected:
            streak += 1
            expected -= timedelta(days=1)
        elif d < expected:
            break
    return streak


def evaluate_day(day: str, data: Dict[str, Any]) -> Dict[str, Any]:
    plan = data.get("plan", DEFAULT_PLAN)
    meals = [m for m in data.get("meals", []) if m.get("date") == day]
    weight_logs = [w for w in data.get("weight_logs", []) if w.get("date") == day]
    sleep_logs = [s for s in data.get("sleep_logs", []) if s.get("date") == day]
    exercise_logs = [e for e in data.get("exercise_logs", []) if e.get("date") == day]
    waist_logs = [w for w in data.get("waist_logs", []) if w.get("date") == day]
    meal_out_window = [m for m in meals if not is_meal_in_window(m.get("time", ""), plan)]
    out_count = len(meal_out_window)

    weight_latest = None
    if weight_logs:
        weight_latest = sorted(weight_logs, key=lambda w: w.get("time", ""), reverse=True)[0].get("weight")

    sleep_latest = None
    if sleep_logs:
        sleep_latest = sorted(sleep_logs, key=lambda s: s.get("time", ""), reverse=True)[0].get("hours")

    exercise_latest = None
    if exercise_logs:
        exercise_latest = sorted(exercise_logs, key=lambda e: e.get("time", ""), reverse=True)[0].get("minutes")

    waist_latest = None
    if waist_logs:
        waist_latest = sorted(waist_logs, key=lambda w: w.get("time", ""), reverse=True)[0].get("cm")

    if out_count > 0:
        status = "未达标"
        reason = "存在进食窗口外进食"
    elif meals:
        if sleep_latest is not None and sleep_latest < 6:
            status = "未达标"
            reason = "睡眠不足，容易影响执行"
        elif exercise_latest is not None and exercise_latest < 20:
            status = "未达标"
            reason = "运动不足，建议补一段快走"
        else:
            status = "达标"
            reason = "当天进食均在窗口内"
    else:
        status = "未记录"
        reason = "当天暂无进食记录"

    return {
        "date": day,
        "status": status,
        "reason": reason,
        "meal_count": len(meals),
        "meal_out_window_count": out_count,
        "fasting_hours": 0.0,
        "fasting_success": False,
        "weight": weight_latest,
        "sleep_hours": sleep_latest,
        "exercise_minutes": exercise_latest,
        "waist_cm": waist_latest,
    }


def period_stats(data: Dict[str, Any], days: int) -> Dict[str, Any]:
    today = now_local().date()
    statuses = []
    for i in range(days):
        d = (today - timedelta(days=i)).strftime(DATE_FMT)
        statuses.append(evaluate_day(d, data))

    ok = sum(1 for s in statuses if s["status"] == "达标")
    fail = sum(1 for s in statuses if s["status"] == "未达标")
    base = ok + fail
    rate = round((ok / base) * 100, 2) if base else 0.0
    return {"days": days, "ok_days": ok, "fail_days": fail, "rate": rate}


def weight_stats(data: Dict[str, Any], days: int) -> Dict[str, Any]:
    logs = sorted(data.get("weight_logs", []), key=lambda w: w.get("time", ""))
    if not logs:
        return {"days": days, "start": None, "current": None, "change": None, "enough": False}

    start_cutoff = now_local() - timedelta(days=days - 1)
    recent = [w for w in logs if parse_time(w.get("time", "1970-01-01 00:00")) >= start_cutoff]
    if len(recent) < 2:
        return {"days": days, "start": None, "current": None, "change": None, "enough": False}

    start = float(recent[0]["weight"])
    current = float(recent[-1]["weight"])
    # Keep "change" aligned with "lost_weight": positive means weight loss.
    return {"days": days, "start": start, "current": current, "change": round(start - current, 2), "enough": True}


def goal_stats(data: Dict[str, Any]) -> Dict[str, Any]:
    goal = data.get("goal", DEFAULT_GOAL)
    logs = sorted(data.get("weight_logs", []), key=lambda w: w.get("time", ""))
    current = float(logs[-1]["weight"]) if logs else None

    start_weight = goal.get("start_weight")
    target_weight = goal.get("target_weight")
    if start_weight is not None:
        start_weight = float(start_weight)
    if target_weight is not None:
        target_weight = float(target_weight)

    start_date_str = str(goal.get("start_date") or "")
    end_date_str = str(goal.get("end_date") or "")
    start_day = None
    end_day = None
    try:
        start_day = datetime.strptime(start_date_str, DATE_FMT).date() if start_date_str else None
    except ValueError:
        start_day = None
    try:
        end_day = datetime.strptime(end_date_str, DATE_FMT).date() if end_date_str else None
    except ValueError:
        end_day = None

    total_days = None
    elapsed_days = None
    remaining_days = None
    days_to_end = None
    time_progress = None
    cycle_status = "unset"
    if start_day and end_day and end_day >= start_day:
        today = now_local().date()
        total_days = (end_day - start_day).days + 1
        elapsed_days = min(total_days, max(0, (today - start_day).days + 1))
        remaining_days = max(0, (end_day - today).days)
        days_to_end = (end_day - today).days
        time_progress = round((elapsed_days / total_days) * 100, 2) if total_days > 0 else 0.0
        if today < start_day:
            cycle_status = "not_started"
        elif today > end_day:
            cycle_status = "ended"
        else:
            cycle_status = "in_progress"
    elif start_day or end_day:
        cycle_status = "invalid"

    lost = None
    left = None
    reached = None
    if start_weight is not None and current is not None:
        lost = round(start_weight - current, 2)
    if target_weight is not None and current is not None:
        left = round(current - target_weight, 2)
        reached = current <= target_weight

    expected_weight = None
    pace_status = "unknown"
    pace_diff = None
    week_target_loss = None
    week_actual_loss = None
    progress_percent = None

    if start_weight is not None and target_weight is not None and total_days and total_days > 0:
        total_loss = start_weight - target_weight
        week_target_loss = round((total_loss / total_days) * 7, 2)

    ws7 = weight_stats(data, 7)
    if ws7.get("enough"):
        week_actual_loss = ws7.get("change")

    if (
        start_weight is not None
        and target_weight is not None
        and current is not None
        and total_days
        and total_days > 0
        and elapsed_days is not None
    ):
        total_loss = start_weight - target_weight
        if total_loss > 0:
            achieved = start_weight - current
            progress_percent = round(max(0.0, min(100.0, (achieved / total_loss) * 100)), 2)
        progress_ratio = elapsed_days / total_days
        expected_weight = round(start_weight - total_loss * progress_ratio, 2)
        pace_diff = round(current - expected_weight, 2)

        if reached:
            pace_status = "reached"
        elif cycle_status == "not_started":
            pace_status = "not_started"
        elif cycle_status == "ended":
            pace_status = "missed"
        elif cycle_status == "in_progress":
            if pace_diff > 0.5:
                pace_status = "behind"
            elif pace_diff < -0.5:
                pace_status = "ahead"
            else:
                pace_status = "on_track"

    return {
        "start_date": start_date_str,
        "end_date": end_date_str,
        "start_weight": start_weight,
        "target_weight": target_weight,
        "current_weight": current,
        "lost_weight": lost,
        "left_weight": left,
        "reached": reached,
        "cycle_status": cycle_status,
        "cycle_total_days": total_days,
        "cycle_elapsed_days": elapsed_days,
        "cycle_remaining_days": remaining_days,
        "cycle_days_to_end": days_to_end,
        "cycle_time_progress": time_progress,
        "is_sprint_phase": bool(cycle_status == "in_progress" and remaining_days is not None and remaining_days <= 14),
        "expected_weight": expected_weight,
        "progress_percent": progress_percent,
        "pace_status": pace_status,
        "pace_diff": pace_diff,
        "week_target_loss": week_target_loss,
        "week_actual_loss": week_actual_loss,
    }


def export_excel_report(data: Dict[str, Any]) -> None:
    records = data.get("records", [])
    meals = data.get("meals", [])
    weights = data.get("weight_logs", [])
    sleeps = data.get("sleep_logs", [])
    exercises = data.get("exercise_logs", [])
    waists = data.get("waist_logs", [])
    success_count = sum(1 for r in records if r.get("success"))
    streak = calc_streak(records)
    active_fast = data.get("active_fast") or ""
    plan = data.get("plan", DEFAULT_PLAN)
    goal = goal_stats(data)

    summary_rows = [
        ["当前进行中断食", active_fast],
        ["进食窗口", f"{plan['start']} + {plan['hours']}小时"],
        ["减肥开始日期", goal.get("start_date") or ""],
        ["减肥结束日期", goal.get("end_date") or ""],
        ["初始体重(kg)", goal.get("start_weight")],
        ["目标体重(kg)", goal.get("target_weight")],
        ["当前体重(kg)", goal.get("current_weight")],
        ["已减重量(kg)", goal.get("lost_weight")],
        ["距目标还差(kg)", goal.get("left_weight")],
        ["周期总天数", goal.get("cycle_total_days")],
        ["周期已过天数", goal.get("cycle_elapsed_days")],
        ["周期剩余天数", goal.get("cycle_remaining_days")],
        ["时间进度(%)", goal.get("cycle_time_progress")],
        ["目标进度(%)", goal.get("progress_percent")],
        ["节奏判定", goal.get("pace_status")],
        ["节奏偏差(kg)", goal.get("pace_diff")],
        ["近7天目标减重(kg)", goal.get("week_target_loss")],
        ["近7天实际减重(kg)", goal.get("week_actual_loss")],
        ["目标达成", "是" if goal.get("reached") else "否"],
        ["累计断食记录天数", len(records)],
        ["达标天数", success_count],
        ["当前连续达标天数", streak],
        ["累计进食记录条数", len(meals)],
        ["累计体重记录条数", len(weights)],
        ["累计睡眠记录条数", len(sleeps)],
        ["累计运动记录条数", len(exercises)],
        ["累计腰围记录条数", len(waists)],
        ["最后导出时间", now_local().strftime(TIME_FMT)],
    ]

    record_rows = [
        [r.get("date", ""), r.get("start", ""), r.get("end", ""), r.get("hours", 0), "达标" if r.get("success") else "未达标", r.get("note", "")]
        for r in sorted(records, key=lambda item: item.get("date", ""), reverse=True)
    ]
    meal_rows = [
        [m.get("date", ""), m.get("time", ""), m.get("food", ""), "是" if is_meal_in_window(m.get("time", ""), plan) else "否", m.get("note", "")]
        for m in sorted(meals, key=lambda item: item.get("time", ""), reverse=True)
    ]
    weight_rows = [
        [w.get("date", ""), w.get("time", ""), w.get("weight", ""), w.get("note", "")]
        for w in sorted(weights, key=lambda item: item.get("time", ""), reverse=True)
    ]
    sleep_rows = [
        [s.get("date", ""), s.get("time", ""), s.get("hours", ""), s.get("note", "")]
        for s in sorted(sleeps, key=lambda item: item.get("time", ""), reverse=True)
    ]
    exercise_rows = [
        [e.get("date", ""), e.get("time", ""), e.get("minutes", ""), e.get("kind", ""), e.get("note", "")]
        for e in sorted(exercises, key=lambda item: item.get("time", ""), reverse=True)
    ]
    waist_rows = [
        [w.get("date", ""), w.get("time", ""), w.get("cm", ""), w.get("note", "")]
        for w in sorted(waists, key=lambda item: item.get("time", ""), reverse=True)
    ]

    today = now_local().date()
    summary_days_rows = [
        [
            s["date"],
            s["status"],
            s["reason"],
            s["meal_count"],
            s["meal_out_window_count"],
            s["fasting_hours"],
            s["weight"],
            s.get("sleep_hours"),
            s.get("exercise_minutes"),
            s.get("waist_cm"),
        ]
        for s in [evaluate_day((today - timedelta(days=i)).strftime(DATE_FMT), data) for i in range(30)]
    ]

    sheet1 = _sheet_xml(["指标", "值"], summary_rows)
    sheet2 = _sheet_xml(["日期", "开始时间", "结束时间", "断食时长(小时)", "结果", "备注"], record_rows)
    sheet3 = _sheet_xml(["日期", "进食时间", "食物", "是否在8小时窗口", "备注"], meal_rows)
    sheet4 = _sheet_xml(["日期", "记录时间", "体重(kg)", "备注"], weight_rows)
    sheet5 = _sheet_xml(
        ["日期", "状态", "原因", "进食次数", "窗口外进食次数", "断食时长", "体重(kg)", "睡眠(小时)", "运动(分钟)", "腰围(cm)"],
        summary_days_rows,
    )
    sheet6 = _sheet_xml(["日期", "记录时间", "睡眠时长(小时)", "备注"], sleep_rows)
    sheet7 = _sheet_xml(["日期", "记录时间", "运动时长(分钟)", "运动类型", "备注"], exercise_rows)
    sheet8 = _sheet_xml(["日期", "记录时间", "腰围(cm)", "备注"], waist_rows)

    content_types = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  <Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
  <Override PartName="/xl/worksheets/sheet2.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
  <Override PartName="/xl/worksheets/sheet3.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
  <Override PartName="/xl/worksheets/sheet4.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
  <Override PartName="/xl/worksheets/sheet5.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
  <Override PartName="/xl/worksheets/sheet6.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
  <Override PartName="/xl/worksheets/sheet7.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
  <Override PartName="/xl/worksheets/sheet8.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
</Types>
"""
    root_rels = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>
"""
    workbook = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets>
    <sheet name="Summary" sheetId="1" r:id="rId1"/>
    <sheet name="FastingRecords" sheetId="2" r:id="rId2"/>
    <sheet name="MealRecords" sheetId="3" r:id="rId3"/>
    <sheet name="WeightRecords" sheetId="4" r:id="rId4"/>
    <sheet name="DailySummary" sheetId="5" r:id="rId5"/>
    <sheet name="SleepRecords" sheetId="6" r:id="rId6"/>
    <sheet name="ExerciseRecords" sheetId="7" r:id="rId7"/>
    <sheet name="WaistRecords" sheetId="8" r:id="rId8"/>
  </sheets>
</workbook>
"""
    workbook_rels = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet2.xml"/>
  <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet3.xml"/>
  <Relationship Id="rId4" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet4.xml"/>
  <Relationship Id="rId5" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet5.xml"/>
  <Relationship Id="rId6" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet6.xml"/>
  <Relationship Id="rId7" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet7.xml"/>
  <Relationship Id="rId8" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet8.xml"/>
</Relationships>
"""

    with zipfile.ZipFile(EXCEL_FILE, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", root_rels)
        zf.writestr("xl/workbook.xml", workbook)
        zf.writestr("xl/_rels/workbook.xml.rels", workbook_rels)
        zf.writestr("xl/worksheets/sheet1.xml", sheet1)
        zf.writestr("xl/worksheets/sheet2.xml", sheet2)
        zf.writestr("xl/worksheets/sheet3.xml", sheet3)
        zf.writestr("xl/worksheets/sheet4.xml", sheet4)
        zf.writestr("xl/worksheets/sheet5.xml", sheet5)
        zf.writestr("xl/worksheets/sheet6.xml", sheet6)
        zf.writestr("xl/worksheets/sheet7.xml", sheet7)
        zf.writestr("xl/worksheets/sheet8.xml", sheet8)


def save_data(data: Dict[str, Any]) -> None:
    with DATA_FILE.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    try:
        export_excel_report(data)
    except Exception as exc:
        print(f"警告: Excel 导出失败: {exc}", file=sys.stderr)


def cmd_start(args: argparse.Namespace) -> None:
    data = load_data()
    if data["active_fast"]:
        print(f"你已经在断食中，开始时间: {data['active_fast']}")
        return

    start_time = parse_time(args.time) if args.time else now_local()
    data["active_fast"] = start_time.strftime(TIME_FMT)
    save_data(data)
    print(f"已开始断食: {data['active_fast']}")


def cmd_end(args: argparse.Namespace) -> None:
    data = load_data()
    if not data["active_fast"]:
        print("当前没有进行中的断食，请先执行 start")
        return

    start = parse_time(data["active_fast"])
    end = parse_time(args.time) if args.time else now_local()
    if end <= start:
        print("结束时间必须晚于开始时间")
        return

    duration = hours_between(start, end)
    success = duration >= args.target
    record = Record(
        date=end.strftime(DATE_FMT),
        start=start.strftime(TIME_FMT),
        end=end.strftime(TIME_FMT),
        hours=duration,
        success=success,
        note=args.note or "",
    )

    data["records"].append(asdict(record))
    data["active_fast"] = None
    save_data(data)

    status = "达标" if success else "未达标"
    day_status = evaluate_day(record.date, data)
    print(f"本次断食 {duration} 小时，目标 {args.target} 小时，结果: {status}")
    print(f"当天状态: {day_status['status']}（{day_status['reason']}）")


def cmd_status(_: argparse.Namespace) -> None:
    data = load_data()
    plan = data.get("plan", DEFAULT_PLAN)
    print(f"今日进食窗口: {plan['start']} + {plan['hours']}小时")
    today = now_local().strftime(DATE_FMT)
    day = evaluate_day(today, data)
    print(f"今日记录进食: {day['meal_count']} 次，窗口外进食: {day['meal_out_window_count']} 次")
    print(f"今日判定: {day['status']}（{day['reason']}）")
    goal = goal_stats(data)
    print(
        f"目标总览: 开始日={goal['start_date'] or '-'} | 初始={goal['start_weight']} kg | "
        f"当前={goal['current_weight']} kg | 目标={goal['target_weight']} kg | 达成={goal['reached']}"
    )


def cmd_checkin(args: argparse.Namespace) -> None:
    data = load_data()
    day = args.date or now_local().strftime(DATE_FMT)

    for r in data["records"]:
        if r["date"] == day:
            print(f"{day} 已存在记录，不重复打卡")
            return

    record = Record(
        date=day,
        start=f"{day} 00:00",
        end=f"{day} 16:00",
        hours=16.0,
        success=True,
        note=args.note or "手动打卡",
    )
    data["records"].append(asdict(record))
    save_data(data)
    print(f"已完成 {day} 打卡")


def cmd_meal(args: argparse.Namespace) -> None:
    data = load_data()
    food = (args.food or "").strip()
    if not food:
        print("food 不能为空，例如: --food \"鸡胸肉+西兰花\"")
        return

    meal_dt = parse_time(args.time) if args.time else now_local()
    meal = MealRecord(
        date=meal_dt.strftime(DATE_FMT),
        time=meal_dt.strftime(TIME_FMT),
        food=food,
        note=(args.note or "").strip(),
    )
    data["meals"].append(asdict(meal))
    save_data(data)

    in_window = is_meal_in_window(meal.time, data.get("plan", DEFAULT_PLAN))
    flag = "窗口内" if in_window else "窗口外"
    print(f"已记录进食: {meal.time} | {meal.food}（{flag}）")


def cmd_meals(args: argparse.Namespace) -> None:
    data = load_data()
    meals = data.get("meals", [])
    if not meals:
        print("暂无进食记录")
        return

    if args.date:
        meals = [m for m in meals if m["date"] == args.date]
        if not meals:
            print(f"{args.date} 暂无进食记录")
            return

    plan = data.get("plan", DEFAULT_PLAN)
    meals = sorted(meals, key=lambda m: m["time"], reverse=True)[: args.limit]
    print(f"最近 {len(meals)} 条进食记录:")
    for m in meals:
        extra = f" | 备注: {m['note']}" if m.get("note") else ""
        flag = "✅窗口内" if is_meal_in_window(m["time"], plan) else "⚠窗口外"
        print(f"{m['time']} | {m['food']} | {flag}{extra}")


def cmd_weight(args: argparse.Namespace) -> None:
    data = load_data()
    if args.value <= 0:
        print("体重必须大于0")
        return

    dt = parse_time(args.time) if args.time else now_local()
    item = WeightRecord(
        date=dt.strftime(DATE_FMT),
        time=dt.strftime(TIME_FMT),
        weight=round(args.value, 2),
        note=(args.note or "").strip(),
    )
    data.setdefault("weight_logs", []).append(asdict(item))
    save_data(data)
    print(f"已记录体重: {item.time} | {item.weight} kg")


def cmd_weights(args: argparse.Namespace) -> None:
    data = load_data()
    logs = data.get("weight_logs", [])
    if not logs:
        print("暂无体重记录")
        return

    if args.date:
        logs = [w for w in logs if w["date"] == args.date]
        if not logs:
            print(f"{args.date} 暂无体重记录")
            return

    logs = sorted(logs, key=lambda w: w["time"], reverse=True)[: args.limit]
    print(f"最近 {len(logs)} 条体重记录:")
    for w in logs:
        extra = f" | 备注: {w['note']}" if w.get("note") else ""
        print(f"{w['time']} | {w['weight']} kg{extra}")


def cmd_set_goal(args: argparse.Namespace) -> None:
    data = load_data()
    if args.start_weight <= 0 or args.target_weight <= 0:
        print("体重必须大于0")
        return
    start_day = datetime.strptime(args.start_date, DATE_FMT).date()
    end_date = args.end_date or (start_day + timedelta(days=61)).strftime(DATE_FMT)
    end_day = datetime.strptime(end_date, DATE_FMT).date()
    if end_day < start_day:
        print("结束日期不能早于开始日期")
        return

    data["goal"] = {
        "start_date": args.start_date,
        "end_date": end_date,
        "start_weight": round(args.start_weight, 2),
        "target_weight": round(args.target_weight, 2),
    }
    save_data(data)
    print(f"已设置减脂目标: 周期={args.start_date}~{end_date}，初始={args.start_weight}kg，目标={args.target_weight}kg")


def cmd_goal(_: argparse.Namespace) -> None:
    data = load_data()
    goal = goal_stats(data)
    print(f"开始日期: {goal['start_date'] or '-'}")
    print(f"结束日期: {goal['end_date'] or '-'}")
    print(f"初始体重: {goal['start_weight']} kg")
    print(f"目标体重: {goal['target_weight']} kg")
    print(f"当前体重: {goal['current_weight']} kg")
    print(f"已减重量: {goal['lost_weight']} kg")
    print(f"距目标还差: {goal['left_weight']} kg")
    print(f"是否达标: {goal['reached']}")
    print(f"周期状态: {goal['cycle_status']}，第{goal['cycle_elapsed_days']}/{goal['cycle_total_days']}天，剩余{goal['cycle_remaining_days']}天")
    print(f"节奏判定: {goal['pace_status']}，偏差: {goal['pace_diff']} kg")



def cmd_set_window(args: argparse.Namespace) -> None:
    data = load_data()
    parse_clock(args.start)
    if args.hours <= 0 or args.hours > 24:
        print("hours 必须在 1-24 之间")
        return

    data["plan"] = {"start": args.start, "hours": int(args.hours)}
    save_data(data)
    print(f"已设置进食窗口: {args.start} + {int(args.hours)}小时")


def cmd_window(_: argparse.Namespace) -> None:
    data = load_data()
    plan = data.get("plan", DEFAULT_PLAN)
    print(f"当前进食窗口: {plan['start']} + {plan['hours']}小时")


def cmd_history(args: argparse.Namespace) -> None:
    data = load_data()
    records = data.get("records", [])
    meals = data.get("meals", [])
    weights = data.get("weight_logs", [])

    print("断食记录:")
    if not records:
        print("暂无断食记录")
    else:
        records_sorted = sorted(records, key=lambda r: r["date"], reverse=True)
        for r in records_sorted[: args.days]:
            status = "✅" if r["success"] else "❌"
            note = f" | 备注: {r['note']}" if r.get("note") else ""
            print(f"{r['date']} | {r['hours']}h | {status}{note}")
        print("-" * 40)
        print(f"累计打卡: {len(records)} 天")
        print(f"达标天数: {sum(1 for r in records if r.get('success'))} 天")
        print(f"当前连续达标: {calc_streak(records)} 天")

    print("\n进食记录:")
    if not meals:
        print("暂无进食记录")
    else:
        plan = data.get("plan", DEFAULT_PLAN)
        grouped: Dict[str, List[Dict[str, Any]]] = {}
        for m in meals:
            grouped.setdefault(m["date"], []).append(m)
        for day in sorted(grouped.keys(), reverse=True)[: args.days]:
            print(f"{day} ({len(grouped[day])}次)")
            for m in sorted(grouped[day], key=lambda x: x["time"]):
                extra = f" | 备注: {m['note']}" if m.get("note") else ""
                flag = "✅" if is_meal_in_window(m["time"], plan) else "⚠"
                print(f"  {m['time']} | {m['food']} | {flag}{extra}")

    print("\n体重记录:")
    if not weights:
        print("暂无体重记录")
    else:
        for w in sorted(weights, key=lambda x: x["time"], reverse=True)[: args.days]:
            extra = f" | 备注: {w['note']}" if w.get("note") else ""
            print(f"{w['time']} | {w['weight']} kg{extra}")

    week = period_stats(data, 7)
    month = period_stats(data, 30)
    ws7 = weight_stats(data, 7)
    ws30 = weight_stats(data, 30)
    goal = goal_stats(data)
    print("\n统计:")
    print(f"近7天达标: {week['ok_days']}天，未达标: {week['fail_days']}天，达标率: {week['rate']}%")
    print(f"近30天达标: {month['ok_days']}天，未达标: {month['fail_days']}天，达标率: {month['rate']}%")
    print(f"近7天体重变化: {ws7['change']} kg")
    print(f"近30天体重变化: {ws30['change']} kg")
    print(f"已减重量: {goal['lost_weight']} kg，距目标还差: {goal['left_weight']} kg，目标达成: {goal['reached']}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="8+16 断食+进食+体重追踪工具")
    sub = parser.add_subparsers(dest="command", required=True)

    p_start = sub.add_parser("start", help="开始断食")
    p_start.add_argument("--time", help=f"开始时间，格式: {TIME_FMT}")
    p_start.set_defaults(func=cmd_start)

    p_end = sub.add_parser("end", help="结束断食")
    p_end.add_argument("--time", help=f"结束时间，格式: {TIME_FMT}")
    p_end.add_argument("--target", type=float, default=16.0, help="目标断食小时数，默认16")
    p_end.add_argument("--note", help="本次断食备注")
    p_end.set_defaults(func=cmd_end)

    p_status = sub.add_parser("status", help="查看当前状态")
    p_status.set_defaults(func=cmd_status)

    p_goal = sub.add_parser("goal", help="查看减脂目标总览")
    p_goal.set_defaults(func=cmd_goal)

    p_set_goal = sub.add_parser("set-goal", help="设置减脂目标")
    p_set_goal.add_argument("--start-date", required=True, help="开始日期，格式 YYYY-MM-DD")
    p_set_goal.add_argument("--end-date", help="结束日期，格式 YYYY-MM-DD，默认开始日期后62天周期")
    p_set_goal.add_argument("--start-weight", required=True, type=float, help="初始体重(kg)")
    p_set_goal.add_argument("--target-weight", required=True, type=float, help="目标体重(kg)")
    p_set_goal.set_defaults(func=cmd_set_goal)

    p_window = sub.add_parser("window", help="查看当前进食窗口")
    p_window.set_defaults(func=cmd_window)

    p_set_window = sub.add_parser("set-window", help="设置每日进食窗口")
    p_set_window.add_argument("--start", required=True, help="窗口开始时间，格式 HH:MM")
    p_set_window.add_argument("--hours", type=int, default=8, help="窗口小时数，默认8")
    p_set_window.set_defaults(func=cmd_set_window)

    p_checkin = sub.add_parser("checkin", help="手动打卡")
    p_checkin.add_argument("--date", help="打卡日期，格式: YYYY-MM-DD")
    p_checkin.add_argument("--note", help="打卡备注")
    p_checkin.set_defaults(func=cmd_checkin)

    p_meal = sub.add_parser("meal", help="记录一次进食")
    p_meal.add_argument("--food", required=True, help="食物内容，例如: 鸡蛋+牛奶")
    p_meal.add_argument("--time", help=f"进食时间，格式: {TIME_FMT}")
    p_meal.add_argument("--note", help="进食备注")
    p_meal.set_defaults(func=cmd_meal)

    p_meals = sub.add_parser("meals", help="查看进食记录")
    p_meals.add_argument("--date", help="仅查看某一天，格式: YYYY-MM-DD")
    p_meals.add_argument("--limit", type=int, default=20, help="最多显示多少条，默认20")
    p_meals.set_defaults(func=cmd_meals)

    p_weight = sub.add_parser("weight", help="记录体重")
    p_weight.add_argument("--value", required=True, type=float, help="体重数值(kg)")
    p_weight.add_argument("--time", help=f"记录时间，格式: {TIME_FMT}")
    p_weight.add_argument("--note", help="体重备注")
    p_weight.set_defaults(func=cmd_weight)

    p_weights = sub.add_parser("weights", help="查看体重记录")
    p_weights.add_argument("--date", help="仅查看某一天，格式: YYYY-MM-DD")
    p_weights.add_argument("--limit", type=int, default=20, help="最多显示多少条，默认20")
    p_weights.set_defaults(func=cmd_weights)

    p_history = sub.add_parser("history", help="查看断食/进食/体重与统计")
    p_history.add_argument("--days", type=int, default=7, help="显示最近几天，默认7")
    p_history.set_defaults(func=cmd_history)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        args.func(args)
    except ValueError as e:
        print(e)


if __name__ == "__main__":
    main()
