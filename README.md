# 减脂执行跟踪程序（8+16）

当前版本聚焦一个目标：帮助个人用户按 8+16 规则稳定执行减脂计划。

支持：

- 固定进食窗口（8小时）执行跟踪
- 每次进食记录（时间、食物、备注）
- 每日体重记录与周期趋势统计
- 目标总览（开始日期、初始体重、目标体重、当前体重、是否达标）
- 教练复盘（睡眠 / 运动 / 餐次建议，不改写 8+16 主执行结果）
- 自动导出 Excel 分析文件
- 可选同步到 Notion（Meals / Weights / Goals / Sleep / Exercise）
- 可选 Telegram 三餐提醒（基于 Notion）

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

首页交互：

- 减脂总览
- 今日状态
- 教练复盘（睡眠 / 运动一键打卡）
- 本周提醒与记录列表（默认最近5条）
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
- `SleepRecords`
- `ExerciseRecords`

## 自动化回归测试

```bash
python3 regression_tests.py
```

测试在临时目录执行，不会污染你当前数据文件。

## 线上部署环境变量

### Railway / Web 服务

- `AUTH_TOKEN`：前端写入保护密钥，建议线上必须配置
- `NOTION_TOKEN`：Notion 集成密钥
- `NOTION_MEALS_DB`：进食表数据库 ID
- `NOTION_WEIGHTS_DB`：体重表数据库 ID
- `NOTION_GOALS_DB`：目标表数据库 ID
- `NOTION_SLEEP_DB`：睡眠表数据库 ID（可选）
- `NOTION_EXERCISE_DB`：运动表数据库 ID（可选）
- `HOST`：可选，默认 `0.0.0.0`
- `PORT`：平台注入，Railway 会自动提供

### GitHub Actions / Telegram 提醒

- `NOTION_TOKEN`
- `NOTION_MEALS_DB`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- `REMINDER_WINDOW`（例如 `07:00-15:00`，可选）
- `TZ`（建议 `Asia/Shanghai`）

## Telegram 提醒（Notion 数据源）

提醒脚本会在固定时间发送三次提醒，帮助你在进食窗口内完成三餐记录。推荐用 GitHub Actions 定时执行。

需要配置的 GitHub Secrets：

- `NOTION_TOKEN`
- `NOTION_MEALS_DB`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- `REMINDER_WINDOW`（例如 `07:00-15:00`，可选）
- `TZ`（建议 `Asia/Shanghai`）

定时任务已在 `.github/workflows/reminder.yml` 中设置为：

- Asia/Shanghai 08:00 / 12:00 / 15:00（对应 UTC 00:00 / 04:00 / 07:00）
