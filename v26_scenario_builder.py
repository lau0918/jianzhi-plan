#!/usr/bin/env python3
"""Build demo data for V2.6 real-device verification scenarios."""

from __future__ import annotations

import argparse
import json
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Dict, List

DATE_FMT = "%Y-%m-%d"
TIME_FMT = "%Y-%m-%d %H:%M"
DATA_FILE = Path("fasting_data.json")
DEFAULT_PLAN = {"start": "07:00", "hours": 8}


def day_str(day: date) -> str:
    return day.strftime(DATE_FMT)


def dt_str(day: date, hhmm: str) -> str:
    hh, mm = hhmm.split(":")
    return datetime.combine(day, time(int(hh), int(mm))).strftime(TIME_FMT)


def meal(day: date, hhmm: str, food: str, note: str = "") -> Dict[str, Any]:
    return {
        "date": day_str(day),
        "time": dt_str(day, hhmm),
        "food": food,
        "note": note,
    }


def weight(day: date, hhmm: str, value: float, note: str = "") -> Dict[str, Any]:
    return {
        "date": day_str(day),
        "time": dt_str(day, hhmm),
        "weight": round(value, 2),
        "note": note,
    }


def scenario_data(code: str, today: date) -> Dict[str, Any]:
    # Keep one stable feeding plan so "in-window/out-window" behavior is predictable.
    plan = dict(DEFAULT_PLAN)

    if code == "A":
        # In-progress + on-track
        start = today - timedelta(days=20)
        end = today + timedelta(days=30)
        goal = {"start_date": day_str(start), "end_date": day_str(end), "start_weight": 100.0, "target_weight": 80.0}
        meals = [
            meal(today - timedelta(days=1), "12:10", "鸡胸肉沙拉"),
            meal(today, "08:20", "燕麦牛奶"),
            meal(today, "13:00", "鱼和蔬菜"),
        ]
        weights = [
            weight(today - timedelta(days=6), "07:10", 93.2),
            weight(today - timedelta(days=3), "07:15", 92.4),
            weight(today, "07:05", 91.7),
        ]
    elif code == "B":
        # Sprint phase (<= 14 days remaining)
        start = today - timedelta(days=45)
        end = today + timedelta(days=10)
        goal = {"start_date": day_str(start), "end_date": day_str(end), "start_weight": 96.0, "target_weight": 82.0}
        meals = [
            meal(today - timedelta(days=1), "11:40", "牛肉蔬菜"),
            meal(today, "08:00", "鸡蛋酸奶"),
            meal(today, "14:15", "豆腐青菜"),
        ]
        weights = [
            weight(today - timedelta(days=6), "07:15", 86.2),
            weight(today - timedelta(days=2), "07:10", 85.7),
            weight(today, "07:00", 85.4),
        ]
    elif code == "C":
        # Ended + not reached
        start = today - timedelta(days=70)
        end = today - timedelta(days=5)
        goal = {"start_date": day_str(start), "end_date": day_str(end), "start_weight": 98.0, "target_weight": 80.0}
        meals = [
            meal(today - timedelta(days=1), "12:30", "鸡胸肉"),
            meal(today, "21:10", "夜宵面包", "窗口外"),
        ]
        weights = [
            weight(today - timedelta(days=6), "07:00", 86.8),
            weight(today - timedelta(days=2), "07:10", 86.5),
            weight(today, "07:05", 86.3),
        ]
    elif code == "D":
        # Ended + reached
        start = today - timedelta(days=70)
        end = today - timedelta(days=5)
        goal = {"start_date": day_str(start), "end_date": day_str(end), "start_weight": 92.0, "target_weight": 78.0}
        meals = [
            meal(today - timedelta(days=1), "11:20", "鱼肉蔬菜"),
            meal(today, "13:40", "鸡蛋牛奶"),
        ]
        weights = [
            weight(today - timedelta(days=6), "07:15", 78.8),
            weight(today - timedelta(days=3), "07:05", 78.2),
            weight(today, "07:00", 77.8),
        ]
    else:
        raise ValueError(f"unsupported scenario: {code}")

    return {
        "active_fast": None,
        "records": [],
        "meals": meals,
        "weight_logs": weights,
        "plan": plan,
        "goal": goal,
    }


def backup_if_needed(target: Path) -> Path | None:
    if not target.exists():
        return None
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = target.with_name(f"{target.stem}.backup_{stamp}{target.suffix}")
    backup.write_text(target.read_text(encoding="utf-8"), encoding="utf-8")
    return backup


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build V2.6 scenario data for real-device testing")
    parser.add_argument("--scenario", choices=["A", "B", "C", "D"], required=True, help="Scenario code from checklist")
    parser.add_argument("--output", default=str(DATA_FILE), help="Output JSON path (default: fasting_data.json)")
    parser.add_argument("--no-backup", action="store_true", help="Do not create backup when output file exists")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    target = Path(args.output)
    data = scenario_data(args.scenario, datetime.now().date())

    backup = None
    if not args.no_backup and target.exists():
        backup = backup_if_needed(target)

    target.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已生成场景 {args.scenario}: {target}")
    if backup:
        print(f"已备份原数据: {backup}")
    print(f"目标周期: {data['goal']['start_date']} ~ {data['goal']['end_date']}")
    print(f"当前体重样本: {data['weight_logs'][-1]['weight']} kg")


if __name__ == "__main__":
    main()

