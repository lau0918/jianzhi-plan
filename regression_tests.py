#!/usr/bin/env python3
"""Regression tests for fasting tracker (CLI + API)."""

from __future__ import annotations

import importlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import zipfile
from datetime import datetime
from http.server import ThreadingHTTPServer
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent
PYTHON = sys.executable


class RegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self._tmp.name)
        self._copy_project_files()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _copy_project_files(self) -> None:
        shutil.copy(PROJECT_ROOT / "fasting_tracker.py", self.tmpdir / "fasting_tracker.py")
        shutil.copy(PROJECT_ROOT / "mobile_server.py", self.tmpdir / "mobile_server.py")
        shutil.copytree(PROJECT_ROOT / "web", self.tmpdir / "web")

    def _run_cli(self, *args: str) -> str:
        cmd = [PYTHON, "fasting_tracker.py", *args]
        result = subprocess.run(
            cmd,
            cwd=self.tmpdir,
            capture_output=True,
            text=True,
            check=True,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
        return result.stdout.strip()

    def test_cli_regression(self) -> None:
        out = self._run_cli("set-goal", "--start-date", "2026-04-01", "--start-weight", "78", "--target-weight", "65")
        self.assertIn("已设置减脂目标", out)

        out = self._run_cli("set-window", "--start", "09:30", "--hours", "8")
        self.assertIn("已设置进食窗口", out)

        out = self._run_cli("meal", "--food", "鸡蛋+牛奶", "--time", "2026-04-08 12:00", "--note", "午餐")
        self.assertIn("已记录进食", out)

        out = self._run_cli("weight", "--value", "72.5", "--time", "2026-04-08 07:30")
        self.assertIn("已记录体重", out)

        out = self._run_cli("history", "--days", "3")
        self.assertIn("体重记录:", out)
        self.assertIn("统计:", out)

        with (self.tmpdir / "fasting_data.json").open("r", encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data.get("goal", {}).get("start_date"), "2026-04-01")
        self.assertEqual(data.get("goal", {}).get("end_date"), "2026-06-01")
        self.assertEqual(data.get("plan", {}).get("start"), "09:30")
        self.assertEqual(len(data.get("records", [])), 0)
        self.assertEqual(len(data.get("meals", [])), 1)
        self.assertEqual(len(data.get("weight_logs", [])), 1)
        self.assertNotIn("waist_logs", data)

        excel_path = self.tmpdir / "fasting_report.xlsx"
        self.assertTrue(excel_path.exists())
        with zipfile.ZipFile(excel_path, "r") as zf:
            names = set(zf.namelist())
        self.assertIn("xl/worksheets/sheet1.xml", names)
        self.assertIn("xl/worksheets/sheet2.xml", names)
        self.assertIn("xl/worksheets/sheet3.xml", names)
        self.assertIn("xl/worksheets/sheet4.xml", names)
        self.assertIn("xl/worksheets/sheet5.xml", names)
        self.assertIn("xl/worksheets/sheet6.xml", names)
        self.assertIn("xl/worksheets/sheet7.xml", names)
        self.assertNotIn("xl/worksheets/sheet8.xml", names)

    def test_day_status_semantics(self) -> None:
        old_cwd = Path.cwd()
        os.chdir(self.tmpdir)
        sys.path.insert(0, str(self.tmpdir))
        try:
            tracker = importlib.import_module("fasting_tracker")
            today = datetime.now().strftime("%Y-%m-%d")
            data = tracker.load_data()
            data["plan"] = {"start": "09:00", "hours": 8}
            data["meals"].append({"date": today, "time": f"{today} 11:00", "food": "鸡蛋", "note": ""})
            data["sleep_logs"].append({"date": today, "time": f"{today} 07:00", "hours": 5.5, "note": ""})
            data["exercise_logs"].append({"date": today, "time": f"{today} 19:00", "minutes": 10, "kind": "步行", "note": ""})

            day = tracker.evaluate_day(today, data)
            week = tracker.period_stats(data, 7)

            self.assertEqual(day["execution_status"], "达标")
            self.assertEqual(day["status"], "达标")
            self.assertEqual(day["execution_reason"], "当天进食均在窗口内")
            self.assertIn("meal_incomplete", day["coach_flags"])
            self.assertIn("sleep_low", day["coach_flags"])
            self.assertIn("exercise_low", day["coach_flags"])
            self.assertEqual(day["coach_status"], "需跟进")
            self.assertEqual(week["ok_days"], 1)
            self.assertEqual(week["fail_days"], 0)
        finally:
            if str(self.tmpdir) in sys.path:
                sys.path.remove(str(self.tmpdir))
            if "fasting_tracker" in sys.modules:
                del sys.modules["fasting_tracker"]
            os.chdir(old_cwd)

    def test_api_regression(self) -> None:
        old_cwd = Path.cwd()
        os.chdir(self.tmpdir)
        sys.path.insert(0, str(self.tmpdir))
        server = None
        thread = None
        old_auth = os.environ.get("AUTH_TOKEN")
        try:
            os.environ["AUTH_TOKEN"] = "test-token"
            mobile_server = importlib.import_module("mobile_server")
            today = datetime.now().strftime("%Y-%m-%d")
            server = ThreadingHTTPServer(("127.0.0.1", 0), mobile_server.TrackerHandler)
            port = server.server_address[1]
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()

            def get(path: str) -> dict:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}") as resp:
                    self.assertEqual(resp.status, 200)
                    return json.loads(resp.read().decode("utf-8"))

            def get_auth(path: str) -> dict:
                req = urllib.request.Request(
                    f"http://127.0.0.1:{port}{path}",
                    headers={"X-Auth-Token": "test-token"},
                    method="GET",
                )
                with urllib.request.urlopen(req) as resp:
                    self.assertEqual(resp.status, 200)
                    return json.loads(resp.read().decode("utf-8"))

            def post(path: str, body: dict) -> dict:
                req = urllib.request.Request(
                    f"http://127.0.0.1:{port}{path}",
                    data=json.dumps(body).encode("utf-8"),
                    headers={"Content-Type": "application/json", "X-Auth-Token": "test-token"},
                    method="POST",
                )
                with urllib.request.urlopen(req) as resp:
                    self.assertEqual(resp.status, 200)
                    return json.loads(resp.read().decode("utf-8"))

            with self.assertRaises(urllib.error.HTTPError) as denied:
                get("/api/status")
            self.assertEqual(denied.exception.code, 401)
            denied_payload = json.loads(denied.exception.read().decode("utf-8"))
            self.assertFalse(denied_payload["ok"])
            self.assertTrue(denied_payload["need_auth"])

            status = get_auth("/api/status")
            self.assertTrue(status["ok"])
            self.assertEqual(status["plan"]["start"], "10:00")

            data = post("/api/window", {"start": "09:00", "hours": 8})
            self.assertTrue(data["ok"])

            data = post("/api/goal", {"start_date": "2026-04-01", "end_date": "2026-06-01", "start_weight": 78, "target_weight": 65})
            self.assertTrue(data["ok"])

            data = post("/api/meal", {"food": "燕麦+牛奶", "time": f"{today} 11:10", "note": "午餐"})
            self.assertTrue(data["ok"])
            self.assertTrue(data["in_window"])

            data = post("/api/weight", {"value": 71.8, "time": f"{today} 07:20", "note": "晨重"})
            self.assertTrue(data["ok"])

            data = post("/api/sleep", {"hours": 5.5, "time": f"{today} 06:50", "note": "一键打卡:睡眠<6h"})
            self.assertTrue(data["ok"])

            data = post("/api/exercise", {"minutes": 10, "time": f"{today} 19:10", "kind": "快走", "note": "一键打卡:运动10分钟"})
            self.assertTrue(data["ok"])

            final_status = get_auth("/api/status")
            self.assertEqual(final_status["plan"]["start"], "09:00")
            self.assertEqual(final_status["goal"]["start_date"], "2026-04-01")
            self.assertEqual(final_status["goal"]["end_date"], "2026-06-01")
            self.assertIn(final_status["goal_source"], ("local", "notion"))
            self.assertIn("cycle_total_days", final_status["goal"])
            self.assertIn("cycle_time_progress", final_status["goal"])
            self.assertIn("pace_status", final_status["goal"])
            self.assertEqual(len(final_status["records"]), 0)
            self.assertEqual(len(final_status["meals"]), 1)
            self.assertEqual(len(final_status["weights"]), 1)
            self.assertEqual(len(final_status["sleeps"]), 1)
            self.assertEqual(len(final_status["exercises"]), 1)
            self.assertNotIn("waists", final_status)
            self.assertIn("coach", final_status)
            self.assertIn("today", final_status)
            self.assertIn("week_stats", final_status)
            self.assertIn("weight_7", final_status)
            self.assertEqual(final_status["today"]["execution_status"], "达标")
            self.assertEqual(final_status["today"]["status"], "达标")
            self.assertEqual(final_status["today"]["coach_status"], "需跟进")
            self.assertIn("meal_incomplete", final_status["today"]["coach_flags"])
            self.assertIn("sleep_low", final_status["today"]["coach_flags"])
            self.assertIn("exercise_low", final_status["today"]["coach_flags"])
            self.assertEqual(final_status["coach"]["status_tone"], "bad")
            self.assertEqual(final_status["week_stats"]["ok_days"], 1)
            self.assertEqual(final_status["week_stats"]["fail_days"], 0)

            self.assertTrue((self.tmpdir / "fasting_report.xlsx").exists())
        finally:
            if server is not None:
                server.shutdown()
                server.server_close()
            if thread is not None:
                thread.join(timeout=2)
            if old_auth is None:
                os.environ.pop("AUTH_TOKEN", None)
            else:
                os.environ["AUTH_TOKEN"] = old_auth
            if str(self.tmpdir) in sys.path:
                sys.path.remove(str(self.tmpdir))
            if "mobile_server" in sys.modules:
                del sys.modules["mobile_server"]
            os.chdir(old_cwd)


if __name__ == "__main__":
    unittest.main(verbosity=2)
