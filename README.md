# SIF 综合处理

这是一个从空项目开始的 Listing 竞品分析 MVP。当前实现的流程是：

```text
产品事实
→ 输入 3-10 个竞品 ASIN
→ SIF MCP 获取竞品画像、流量关键词和 ABA 卡位词
→ 关键词清洗、分类和 P0-P4 分层
→ 选择决策策略（综合增长 / SEO Ranking Growth / Conversion First / New Product Differentiation / Listing Refresh）
→ GPT 生成消费者画像
→ 生成标题、五点、Q&A、模拟买家评论和 Alexa 购物场景
```

决策策略会保存在每条任务和历史记录中。SIF 抓取和基础关键词池对 5 种策略共用，GPT 将所选方案作为内部人设和分析规则，最终统一输出消费者画像、标题、五点、Q&A 和购物场景，不再单独展示“策略输出”栏目。历史任务没有策略字段时按“综合增长”兼容处理。

## SIF MCP 接入

SIF 官方 MCP 使用 Streamable HTTP，地址形如：

```text
https://mcp.sif.com/mcp?secret-key=你的SIF密钥
```

本项目的后端使用以下工具：

- `market_get_asin_profile`
- `market_get_asin_keyword_signals`
- `market_get_asin_aba_footprint`

密钥只放在服务器端 `.env`，不会发送到浏览器。

## Windows 启动

在 PowerShell 中执行：

```powershell
cd 'E:\桌面\SIF综合处理'
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
notepad .env
uvicorn backend.app:app --host 127.0.0.1 --port 8090
```

打开 `http://127.0.0.1:8090`。

## 注册、历史与后台

项目现在包含独立的账号体系：

- 未登录访问首页会进入 `/static/auth.html`，支持注册、登录和退出。
- 普通用户只能查看自己创建的分析任务，可在 `/static/history.html` 查看历史并导出单条或全部 JSON。
- 首页历史分析栏按时间倒序展示，每页 10 条；超过 10 条时可使用上一页/下一页翻页。
- 分析结果中的消费者画像、标题、五点、Q&A、模拟买家评论和购物场景均可单独下载为 UTF-8 文本文件；下载内容取当前页面最新结果，包含人工保留、删除或编辑后的内容。模拟买家评论按“英文标题、中文标题、英文评论、中文翻译”成组输出，旧的纯文本历史评论也可兼容查看。
- 关键词结果表不再展示或计算流量占比；消费者画像使用中文，标题/五点/Q&A 同时展示英文原文和中文翻译。Q&A 可指定重新生成 1-50 条，并按每页最多 10 条翻页查看。
- 管理员可在 `/static/admin.html` 管理用户、启用/禁用账号、重置密码，并查看接口请求记录。
- 分析任务、状态、结果和请求日志持久化到 `data/sif.sqlite3`，服务重启后仍可查询。
- 分析结果页的“重新生成 GPT 内容”会复用该任务已保存的 SIF 数据，只重新调用 GPT 生成消费者画像、标题、五点、Q&A 和购物场景，不会再次调用 SIF 或消耗 SIF 点数；每次重生成会创建一个新的历史任务版本。
- 候选卖点会随任务结果一起保留并显示在分析结果页的“候选卖点”区域（含每个卖点的说明和依据）；运营人员可重新勾选卖点后点击“按所选卖点重新生成”，按新的卖点组合再次生成内容，同样复用原 SIF 数据。

首次启动会根据 `.env` 中的 `ADMIN_USERNAME` 和 `ADMIN_PASSWORD` 自动创建管理员。未配置时兼容默认账号 `admin / ChangeMe123!`，部署后请立即修改密码。生产环境请使用 HTTPS，并将数据库文件纳入备份。

新增接口：

- `POST /api/auth/register`、`POST /api/auth/login`、`POST /api/auth/logout`、`GET /api/auth/me`
- `GET /api/history`、`GET /api/history/{job_id}/export`
- `GET /api/admin/users`、`PATCH /api/admin/users/{user_id}`、`GET /api/admin/requests`

调用 GPT 时可在创建任务页面选择“官网”“中转站”或“自动故障切换”，默认使用官网；模型可按任务填写，默认是 `gpt-5.6-sol`。节点和模型会随任务保存，整页重新生成及标题/五点/Q&A/评论的单独重新生成都会沿用原任务配置。

中转站配置：将 `OPENAI_BASE_URL` 填为 OpenAI 兼容接口的根地址（通常以 `/v1` 结尾），并根据中转站性能调整 `OPENAI_TIMEOUT_SECONDS`；完整竞品分析默认允许等待 120 秒。GPT 遇到超时、连接异常、限流或 5xx 临时错误时，会按 `OPENAI_MAX_RETRIES` 再请求，重试间隔由 `OPENAI_RETRY_DELAY_SECONDS` 控制并采用指数退避。修改 `.env` 后需要重启 Uvicorn。

GPT 故障切换：选择“自动故障切换”且同时配置两套密钥时，系统先调用中转站；中转站完成重试后仍出现超时、连接异常、限流或 5xx 错误，才调用官网。选择“官网”或“中转站”时只调用所选节点，不会自动切换。两个节点都失败时才使用本地兜底结果。

任务并发：`ANALYSIS_CONCURRENCY` 控制同时执行的 SIF/GPT 分析数量，默认是 `2`。运营人员可以连续提交多个产品，任务会立即写入历史并进入 FIFO 队列；达到并发上限的任务显示为“排队中”，空闲 worker 会自动继续处理。服务重启后，数据库中未完成的任务会自动重新排队。中转站有严格限流时可将该值调为 `1`，性能充足时再逐步调高。

GPT 联调脚本：

```powershell
# 只测当前中转站，并使用演示 SIF 数据跑标题/五点/Q&A
.\\.venv\\Scripts\\python.exe scripts\\test_gpt.py --gpt-only

# 额外测试 OpenAI 官网直连（需在 .env 配置 OPENAI_OFFICIAL_API_KEY）
.\\.venv\\Scripts\\python.exe scripts\\test_gpt.py --gpt-only --official-gpt

# 只检测中转站和官网连通性，不执行内容生成
.\\.venv\\Scripts\\python.exe scripts\\test_gpt.py --official-gpt --probe-only

# 真实 SIF + 中转站 GPT 全链路
.\\.venv\\Scripts\\python.exe scripts\\test_gpt.py --asins B0XXXXXXXX B0YYYYYYYY B0ZZZZZZZZ
```

如果暂时没有 SIF 或 GPT 密钥，可以在页面勾选“使用演示数据”，或者保持 `.env` 中的密钥为空，系统会使用本地演示分析。配置密钥后再使用真实链路。

## 亚马逊养号网站集成

SIF 综合处理现已支持将生成的模拟买家评论直接上传到亚马逊养号网站的直评任务表，方便运营团队统一管理和执行。

### 配置方法

在 `.env` 文件中添加以下配置：

```env
# 亚马逊养号网站集成配置（可选）
YMX_API_URL=http://localhost/ymx.com/api/import_reviews_from_sif.php
YMX_API_KEY=sif-import-key-2026
YMX_ENABLED=true
```

- `YMX_API_URL`：养号网站的评论导入API地址
- `YMX_API_KEY`：API密钥（需与养号网站配置一致）
- `YMX_ENABLED`：设为 `true` 启用集成，`false` 禁用

### 使用方法

1. 完成竞品分析并生成模拟买家评论后
2. 在分析结果页的”模拟买家评论”栏目中点击”上传到养号网站”按钮
3. 确认后，系统会将所有评论上传到养号网站的 `direct_review` 表，任务状态为 `pending`（待处理）
4. 养号网站的 Python 自动化工具会自动获取并执行这些直评任务

### 注意事项

- 上传的评论会自动包含中英文标题和内容，并标注为”SIF自动导入”
- 默认星级为 5 星，任务日期为当天
- 上传成功后会显示成功/失败数量和详情
- 养号网站需要先配置好相应的 API 接口（已提供）

## 生产化前需要补充

- 把内存任务表替换为 MySQL/Redis 队列，避免服务重启丢任务。
- 如需与 `ymx.com` 共享账号，再将两个项目的用户表和会话统一；当前 SIF 项目使用独立 SQLite 账号库。
- 对产品事实、AI 版本、人工修改和最终导出增加审计记录。
- 对 SIF 调用做预算、限流和失败重试；不要把 SIF 数据做成对外数据服务。
- “内部模拟买家反馈”只能用于内容研究，不能冒充真实顾客评价发布。
