# AI 日报

一份**每天自动生成**的中文 AI 日报：服务端用 DeepSeek Search 检索多维度信息，用 Fetch 抓取正文，再用 `deepseek-chat` 完成选题、摘要与成稿，最终以美观的网页形式呈现**前一天**的 AI 动态。

- 访问地址：`http://localhost:8000`
- 自动生成：**服务启动时**立即生成一次 + 每天**北京时间 07:00** 重新生成
- 全流程容错：Search / Fetch / 成稿任一步失败，页面仍可展示降级日报或上一次缓存

---

## 1. 快速开始

### 1.1 环境要求

- Python 3.10+（代码使用 `zoneinfo`，3.9 需额外安装 `tzdata`）
- 可访问 `api.deepseek.com` 的网络
- 一个有效的 DeepSeek API Key（[platform.deepseek.com](https://platform.deepseek.com)）

### 1.2 安装

```bash
cd ai-daily

# 创建并激活虚拟环境
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

# 安装依赖
pip install -r requirements.txt
```

### 1.3 配置 API Key

```bash
cp .env.example .env
```

编辑 `.env`，填入你的 Key（**Key 只从环境变量读取，不会出现在代码或前端页面中**）：

```ini
DEEPSEEK_API_KEY=sk-xxxxxxxxxxxxxxxxxxxxxxxx
```

也可以不用 `.env`，直接导出环境变量：

```bash
export DEEPSEEK_API_KEY=sk-xxxxxxxxxxxxxxxxxxxxxxxx
```

### 1.4 启动

```bash
python app.py
```

看到如下日志即表示启动成功（首次生成需要 1–3 分钟，页面会自动刷新）：

```
2026-06-02 09:12:03 [INFO] __main__/MainThread: 启动 AI 日报服务：http://0.0.0.0:8000
2026-06-02 09:12:03 [INFO] scheduler/MainThread: 调度器已启动，时区 Asia/Shanghai，下一次运行时间：2026-06-03 07:00:00+08:00
2026-06-02 09:12:03 [INFO] scheduler/MainThread: 已在后台线程触发启动时日报生成
```

浏览器打开 <http://localhost:8000>。

> 未配置 Key 时，服务仍会启动并展示一份明确标注为“演示数据”的示例日报，便于先预览样式；配置 Key 后重启即生成真实日报。

---

## 2. 生成流程（四阶段）

目标日期 `target_date = 北京时间今天 − 1 天`，全部日期计算使用 `zoneinfo.ZoneInfo("Asia/Shanghai")`。

| 阶段 | 模块 | 做什么 |
| --- | --- | --- |
| 1. 并行 Search | `services/search.py` | 4 个维度查询并发执行（模型与产品 / 行业与融资 / 开源与工具 / 政策与治理），`count` 10–12，`need_content=false`，用 `after:` 收窄时间窗，合并候选池 |
| 2. 机械筛选 | `services/daily_report.py` | 阈值过滤（`rerank_score≥1.5` 且 `authority≥3`，不足 8 条时放宽至 `≥1.0`/`≥2`）→ 日期精确过滤 → URL 去重 → 标题二元组相似度去重（>0.85 保留权威度高者）→ 截取 8–12 条 |
| 3. 选题 + 深读 | `services/llm.py`、`services/fetch.py` | 模型从候选中选出 4–6 条（严格 JSON）；`ThreadPoolExecutor(max_workers=5)` 并发 Fetch；正文超 8000 字符时生成 300–500 字事实摘要 |
| 4. 约束成稿 | `services/llm.py` | 按固定 JSON Schema 成稿，800–1200 字，按五类分组；解析失败自动降级为规则拼接 |

**关键约束（写在 System Prompt 中）**：只能使用给定素材、必须保留原始 URL 与日期、不得引入外部知识、`authority<3` 或单一来源标注“待核实”、抓取失败条目标注“未能抓取全文”、只输出 JSON 不输出 Markdown 代码块。

成稿后服务端还会做一次**白名单校验**（`_sanitize_report`）：分类必须在五类之内，URL 必须在素材列表中出现过，日期与 authority 以素材为准，防止模型改写链接或编造数据。

---

## 3. 调度

`config.TIMEZONE_NAME = "Asia/Shanghai"`，`scheduler.py`：

- `BackgroundScheduler(timezone="Asia/Shanghai")` + `CronTrigger(hour=7, minute=0)`
- 任务 `daily_ai_report` 每天 07:00 调用 `generate_daily_report()`
- 应用启动时另起 `threading.Thread(..., daemon=True)` 立即生成一次
- 生成函数内部有互斥锁，启动生成与定时任务不会并发写缓存
- `app.run(debug=False, use_reloader=False)`，避免 Flask 重载导致调度器重复启动

---

## 4. 缓存与容错

| 场景 | 行为 |
| --- | --- |
| 生成成功 | 写入 `data/reports/{YYYY-MM-DD}.json`，同时覆盖 `data/latest.json`（原子写，先写 `.tmp` 再 `os.replace`） |
| 无缓存且正在生成 | 展示“日报生成中”占位页，每 5 秒自动刷新 |
| Search 全部失败 | **保留上一次** `latest.json`，页面顶栏提示“本次更新失败，展示上次日报” |
| 单条 Fetch 失败（403/404/408/超时） | 不重试同一 URL（408/429/5xx 仅重试一次，间隔 1 秒），降级使用 Search 摘要并标注“未能抓取全文”；无摘要则丢弃该条 |
| 模型成稿失败 / JSON 不合法 | 用阶段 3 素材按类别拼接降级日报，页面标注“降级排版” |
| 未配置 API Key | 生成带“演示数据”标记的示例日报，页面顶部提示配置方式 |

生成日志示例（同时输出到控制台与 `data/app.log`）：

```
2026-06-02 09:14:12 [INFO] services.daily_report/initial-report-generation:
阶段1 Search：查询 4 条（成功 4 / 失败 0），候选 43 条
阶段2 筛选：候选 43 → 阈值后 21 → 日期后 15 → URL 去重 13 → 标题去重 12 → 最终 12（放宽=False）
阶段3 选题：选中 5 条
阶段3 Fetch：成功 4 / 失败 1 / 丢弃 0
2026-06-02 09:14:12 [INFO] services.daily_report/initial-report-generation:
阶段4 完成：目标日期=2026-06-01 查询数=4 候选数=12 选中数=5 Fetch成功=4 Fetch失败=1 条目数=5 降级=False 耗时=68.42s
```

---

## 5. HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/` | 渲染最新日报页面（无缓存时展示生成中/失败占位页） |
| GET | `/api/report` | 返回 `data/latest.json` 内容 |
| GET | `/api/status` | `{status, last_generated_at, target_date, item_count, message}`，`status` 取值 `generating` / `ready` / `error` / `idle` |
| GET | `/healthz` | 存活探针 |

```bash
curl -s http://localhost:8000/api/status | python3 -m json.tool
curl -s http://localhost:8000/api/report | python3 -m json.tool | head -40
```

---

## 6. 项目结构

```
ai-daily/
├── app.py                 # Flask 入口：路由、日志、启动引导
├── config.py              # 端点、阈值、路径、时区，全部可被环境变量覆盖
├── scheduler.py           # APScheduler 07:00 定时 + 启动后台生成
├── requirements.txt
├── .env.example
├── README.md
├── services/
│   ├── __init__.py
│   ├── search.py          # 阶段 1：多主题并行 Search
│   ├── fetch.py           # 阶段 3：Fetch 抓取 + 重试策略
│   ├── llm.py             # 选题 / 长文摘要 / 成稿 + JSON 稳健解析
│   └── daily_report.py    # 阶段 2/4 编排、缓存、状态、降级
├── templates/
│   └── index.html         # 日报页 + 生成中/失败占位页
├── static/
│   └── style.css          # 手写 CSS（浅灰背景 + 白卡片 + 12px 圆角）
└── data/
    ├── reports/           # 每日归档：{target_date}.json
    ├── latest.json        # 最新日报（运行时生成）
    └── app.log            # 运行日志（运行时生成，2MB 轮转 3 份）
```

---

## 7. 可配置项（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DEEPSEEK_API_KEY` | 空 | 必填，缺失时进入演示模式 |
| `DEEPSEEK_SEARCH_URL` | `https://api.deepseek.com/v1/tools/search` | 按官方文档，可覆盖 |
| `DEEPSEEK_FETCH_URL` | `https://api.deepseek.com/v1/tools/fetch` | 按官方文档，可覆盖 |
| `DEEPSEEK_CHAT_URL` | `https://api.deepseek.com/chat/completions` | OpenAI 兼容 |
| `DEEPSEEK_CHAT_MODEL` | `deepseek-chat` | 模型名 |
| `HOST` / `PORT` | `0.0.0.0` / `8000` | 监听地址 |
| `DEMO_MODE` | `0` | 置 1 时强制生成演示日报 |
| `STRICT_RERANK_SCORE` / `STRICT_AUTHORITY` | `1.5` / `3` | 严格筛选阈值 |
| `MAX_CANDIDATES` | `12` | 候选上限 |
| `SELECT_MIN_ITEMS` / `SELECT_MAX_ITEMS` | `4` / `6` | 选题数量区间 |
| `FETCH_MAX_CHARS` | `8000` | 超过则触发长文摘要 |

> Search / Fetch 端点默认值取自 DeepSeek 官方文档《网页搜索与抓取》：`POST /v1/tools/search`、`POST /v1/tools/fetch`。若官方调整路径，只需改环境变量，无需改代码。

---

## 8. 常见问题

**页面一直显示“日报生成中”？**
查看控制台或 `data/app.log`。最常见原因是 Key 未生效（`DEEPSEEK_API_KEY` 为空）或账户余额不足（HTTP 402）。日志中会有明确的 `Search 失败` / `Fetch 失败` 记录。

**日报条目少于 4 条？**
说明当天命中的高质量结果偏少，或多数 Fetch 被目标站点拦截。可放宽 `STRICT_AUTHORITY`（如改为 2）或提高 `SEARCH_COUNT_DEFAULT`。

**定时任务没触发？**
确认进程一直运行（`BackgroundScheduler` 随主进程存活），且系统时间正确；任务时区固定为 `Asia/Shanghai`，与服务器本地时区无关。

**手动重新生成一次？**
重启服务即可（启动即生成）；或在 Python 交互中执行：

```bash
python -c "from app import configure_logging; from services.daily_report import generate_daily_report; configure_logging(); generate_daily_report()"
```
