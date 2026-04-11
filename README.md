# 减脂执行跟踪程序（8+16）

支持：

- 固定进食窗口（8小时）执行跟踪
- 每次进食记录（时间、食物、备注）
- 每日体重记录与趋势统计
- 减脂目标总览（开始日期、初始体重、目标体重、当前体重、是否达标）
- 自动导出 Excel 分析文件
- 可选同步到 Notion（Meals / Weights / Goals）
- 可选 Telegram 提醒（基于 Notion）

## 命令行使用

```bash
python3 fasting_tracker.py --help
```

常用命令：

```bash
# 设置减脂目标（初始体重手动固定）
python3 fasting_tracker.py set-goal --start-date "2026-04-01" --start-weight 78 --target-weight 65
python3 fasting_tracker.py goal

# 设置进食窗口（固定时段）
python3 fasting_tracker.py set-window --start "07:00" --hours 8
python3 fasting_tracker.py window

# 记录进食 / 体重
python3 fasting_tracker.py meal --food "鸡胸肉+西兰花" --time "2026-04-08 12:10"
python3 fasting_tracker.py weight --value 71.8 --time "2026-04-08 07:20"

# 查看历史与统计
python3 fasting_tracker.py history --days 14
```

## 手机网页（鸿蒙推荐）

启动服务：

```bash
python3 mobile_server.py --host 0.0.0.0 --port 8000
```

手机访问：

1. 电脑和手机在同一 Wi-Fi。
2. 查电脑局域网 IP（如 `192.168.1.8`）。
3. 手机浏览器打开 `http://192.168.1.8:8000`。

首页交互（极简）：

- 减脂总览
- 今日状态
- 两个主按钮：记录进食、记录体重
- 记录列表（进食/体重，默认最近5条）
- 设置中统一编辑目标和进食窗口

## 数据文件

- `fasting_data.json`：主数据
- `fasting_report.xlsx`：自动分析报表（每次保存后自动刷新）

Excel 工作表：

- `Summary`
- `FastingRecords`
- `MealRecords`
- `WeightRecords`
- `DailySummary`

## 自动化回归测试

```bash
python3 regression_tests.py
```

测试在临时目录执行，不会污染你当前数据文件。

## Telegram 提醒（Notion 数据源）

当当天还没有进食记录时，按固定时间发送提醒。推荐用 GitHub Actions 定时执行。

需要配置的 GitHub Secrets：

- `NOTION_TOKEN`
- `NOTION_MEALS_DB`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- `REMINDER_WINDOW`（例如 `07:00-15:00`，可选）

定时任务已在 `.github/workflows/reminder.yml` 中设置为：

- Asia/Shanghai 08:00 / 12:00 / 15:00（对应 UTC 00:00 / 04:00 / 07:00）
