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
import urllib.request
import zipfile
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

        out = self._run_cli("start", "--time", "2026-04-08 20:00")
        self.assertIn("已开始断食", out)

        out = self._run_cli("meal", "--food", "鸡蛋+牛奶", "--time", "2026-04-08 21:00", "--note", "加餐")
        self.assertIn("已记录进食", out)

        out = self._run_cli("weight", "--value", "72.5", "--time", "2026-04-08 07:30")
        self.assertIn("已记录体重", out)

        out = self._run_cli("end", "--time", "2026-04-09 12:30", "--target", "16", "--note", "回归测试")
        self.assertIn("结果: 达标", out)

        out = self._run_cli("history", "--days", "3")
        self.assertIn("体重记录:", out)
        self.assertIn("统计:", out)

        with (self.tmpdir / "fasting_data.json").open("r", encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data.get("goal", {}).get("start_date"), "2026-04-01")
        self.assertEqual(data.get("goal", {}).get("end_date"), "2026-06-01")
        self.assertEqual(data.get("plan", {}).get("start"), "09:30")
        self.assertEqual(len(data.get("records", [])), 1)
        self.assertEqual(len(data.get("meals", [])), 1)
        self.assertEqual(len(data.get("weight_logs", [])), 1)

        excel_path = self.tmpdir / "fasting_report.xlsx"
        self.assertTrue(excel_path.exists())
        with zipfile.ZipFile(excel_path, "r") as zf:
            names = set(zf.namelist())
        self.assertIn("xl/worksheets/sheet1.xml", names)
        self.assertIn("xl/worksheets/sheet2.xml", names)
        self.assertIn("xl/worksheets/sheet3.xml", names)
        self.assertIn("xl/worksheets/sheet4.xml", names)
        self.assertIn("xl/worksheets/sheet5.xml", names)

    def test_api_regression(self) -> None:
        old_cwd = Path.cwd()
        os.chdir(self.tmpdir)
        sys.path.insert(0, str(self.tmpdir))
        server = None
        thread = None
        try:
            mobile_server = importlib.import_module("mobile_server")
            server = ThreadingHTTPServer(("127.0.0.1", 0), mobile_server.TrackerHandler)
            port = server.server_address[1]
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()

            def get(path: str) -> dict:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}") as resp:
                    self.assertEqual(resp.status, 200)
                    return json.loads(resp.read().decode("utf-8"))

            def post(path: str, body: dict) -> dict:
                req = urllib.request.Request(
                    f"http://127.0.0.1:{port}{path}",
                    data=json.dumps(body).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req) as resp:
                    self.assertEqual(resp.status, 200)
                    return json.loads(resp.read().decode("utf-8"))

            status = get("/api/status")
            self.assertTrue(status["ok"])
            self.assertEqual(status["plan"]["start"], "10:00")

            data = post("/api/window", {"start": "09:00", "hours": 8})
            self.assertTrue(data["ok"])

            data = post("/api/goal", {"start_date": "2026-04-01", "end_date": "2026-06-01", "start_weight": 78, "target_weight": 65})
            self.assertTrue(data["ok"])

            data = post("/api/start", {"time": "2026-04-08 20:00"})
            self.assertTrue(data["ok"])

            data = post("/api/meal", {"food": "燕麦+牛奶", "time": "2026-04-08 21:10", "note": "晚餐"})
            self.assertTrue(data["ok"])

            data = post("/api/weight", {"value": 71.8, "time": "2026-04-08 07:20", "note": "晨重"})
            self.assertTrue(data["ok"])

            data = post("/api/end", {"time": "2026-04-09 12:30", "target": 16, "note": "API回归"})
            self.assertTrue(data["ok"])
            self.assertTrue(data["success"])

            final_status = get("/api/status")
            self.assertEqual(final_status["plan"]["start"], "09:00")
            self.assertEqual(final_status["goal"]["start_date"], "2026-04-01")
            self.assertEqual(final_status["goal"]["end_date"], "2026-06-01")
            self.assertIn("cycle_total_days", final_status["goal"])
            self.assertIn("cycle_time_progress", final_status["goal"])
            self.assertIn("pace_status", final_status["goal"])
            self.assertEqual(len(final_status["records"]), 1)
            self.assertEqual(len(final_status["meals"]), 1)
            self.assertEqual(len(final_status["weights"]), 1)
            self.assertIn("today", final_status)
            self.assertIn("week_stats", final_status)
            self.assertIn("weight_7", final_status)

            self.assertTrue((self.tmpdir / "fasting_report.xlsx").exists())
        finally:
            if server is not None:
                server.shutdown()
                server.server_close()
            if thread is not None:
                thread.join(timeout=2)
            if str(self.tmpdir) in sys.path:
                sys.path.remove(str(self.tmpdir))
            if "mobile_server" in sys.modules:
                del sys.modules["mobile_server"]
            os.chdir(old_cwd)


if __name__ == "__main__":
    unittest.main(verbosity=2)
