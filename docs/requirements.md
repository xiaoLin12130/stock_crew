# Stock Review Crew 全量重构 — 需求规格 v1（2026-08-03 用户逐条确认）

> 本文为唯一契约。草案存档：`docs/requirements-draft.md`；与本文冲突的以本文为准。
> 原则：**不逐项打补丁**，按本文档完成「设计 → 开发 → 测试 → 冒烟 → 验收」整轮交付。
> 目标仓库：[xiaoLin12130/stock_crew.git](https://github.com/xiaoLin12130/stock_crew.git)

## 〇、用户确认记录（Q1-Q10）

| # | 结论 | 落地 |
|---|---|---|
| Q1 | 开盘啦 Cookie：用户将按方法提供 | `.env` 增加 `KAIPANLA_COOKIE`；未提供前竞价数据走降级链并标注；提供方法见 §四.5 |
| Q2 | Tushare token 允许移入 `.env` | `config.py` 删除硬编码 token，仅读环境变量；`.env` 补齐 |
| Q3 | 通达信路径 `F:\tdx`（已确认存在，含 vipdoc sh/sz/bj） | 新增通达信本地数据源（日线/分时文件），配置项 `TDX_PATH` |
| Q4 | 代理 `127.0.0.1:7890` 可用 | git push / uv add / 外部 API 走代理；启动代理客户端后验证 |
| Q5 | 数据源优先级与降级链按草案 | §四.3 |
| Q6 | **需要盘中模式** | 模式表扩为 6 种：`pre_market / auction / intraday_am / noon / intraday_pm / close` |
| Q7 | 聊天三档全做 | 单分析师 + 多分析师交叉 + 历史对话检索（复用 chroma） |
| Q8 | 聊天入口按 Codex 风格 | 内容区首页卡片 + 侧栏「对话记录」分组 |
| Q9 | 首页布局按 Codex 风格 | 复盘表单 + 聊天卡片 + 历史分组 |
| Q10 | 需要旧 output/ 导入脚本 | `scripts/import_output_history.py` 迁入 `data/reviews/` |

## 一、业务功能

### 1.1 前端（React + Vite + ECharts，Codex 桌面风格）
- 深色侧栏 `#17181D`、内容区 `#F7F7F5`、强调绿 `#10A37F`、圆角卡片、细边框、全中文；
- 侧栏可收起/展开（收起后 brand 图标 = 恢复入口）；「新建复盘」为侧栏唯一主按钮；
- 首页：复盘表单（日期 + 时间点模式选择）+ 聊天卡片（选标的/分析师）+ 历史分组；
- 复盘任务分步进度条（取数→资讯→趋势派→情绪派→主持人→辩论→报告→完成），轮询 job，
  禁止白屏；fetch 30s 超时 + 中文错误；上传/选择控件支持重复操作；
- 结果页：报告 Markdown + 分析师折叠卡 + 辩论记录 + 数据快照图表（ECharts 中文 tooltip）；
- **红涨绿跌**：正收益红 `#E03131` 系、负收益绿 `#0CA678` 系、零值中性灰；警示/免责声明框不参与；
- 历史按日期分组，同日多份按时间点展示（如「08:50 早盘前决策」「09:20 竞价复盘」
  「10:30 上午盘中」「12:30 午间复盘」「14:00 下午盘中」「15:05 收盘复盘」），删除需二次确认；
- 空态/加载/错误态全覆盖；字段映射走 normalize 层 + 契约断言脚本。

### 1.2 后端（FastAPI）
- 异步任务（线程 + 进度回调 + 轮询接口）；历史接口（按日期分组 / 按日期+时间点详情 / 删除单份）；
- 静态托管 `frontend/dist`；`GET /api/health`；错误一律中文；参数校验 400/413/404 语义明确。

### 1.3 复盘引擎（LangGraph 保留）
- **6 种时间模式**（§二）：模式决定取数节点与提示词模板，分析师流程复用
  （并行点评 → 主持人 → 分歧 → 辩论循环 ≤3 → 汇总报告）；
- 自动注入历史上下文：昨日收盘复盘 + 当日更早时间点复盘；
- 输出：`final_report`（唯一 Markdown）、`analysts[]`、`debate_history[]`、
  `overall_tags[]`、`degraded`、`disclaimer`；
- LLM 无 Key/失败 → 规则引擎降级（`degraded=True` 可见标注）；数据缺失 → 明确标注，绝不静默。

### 1.4 分析师聊天（功能 B，三档全做）
- 选择标的（个股/板块）与分析师（单人或多人）多轮对话；
- 上下文 = skill 人格 + 标的实时/历史数据 + chroma 相关复盘结论 + 历史消息；
- 多分析师交叉问答：对上一轮回答逐位表态（同意/反对/补充）后汇总；
- 对话持久化 `data/chats/`，可按标的/日期查看历史会话，支持删除；
- 历史对话检索：chroma 新增 `chat_history` 集合（或扩展 review_history）；
- 免责声明程序级强制：回复组装层追加，响应恒含 `disclaimer`，前端固定横幅。

### 1.5 历史存储（同一天多份）
- `data/reviews/{YYYY-MM-DD}/{HHMMSS}/`：`meta.json + report.json + snapshot.json`；
- 列表按日期倒序、同日按时间点倒序；详情按日期+时间点；删除单份（204/404）；
- 复盘自动引用历史：昨日收盘复盘 + 当日更早时间点（`/api/reviews/context` 注入）；
- **旧 `output/` 导入脚本**：`scripts/import_output_history.py` 把
  `output/{date}/复盘_{time}.md` 导入为 `data/reviews/{date}/{time}/`（模式按时间点推断，
  快照缺失标注 `imported_from_legacy`），幂等可重跑。

## 二、复盘时间模式定义（6 模式，Q6 确认）

| 模式 | 代码 | 适用时间 | 数据输入 | 输出 |
|---|---|---|---|---|
| 早盘前决策 | `pre_market` | 当日 09:15 前 | 隔夜外盘（纳指/富时A50）、昨夜消息、昨日/前日收盘复盘、指数日线均线 | 今日大盘预判 + 操作计划 |
| 竞价复盘 | `auction` | 09:15–09:25 | 竞价数据：高开/低开幅度、竞价金额、抢筹/砸盘、热门股竞价异动 | 早盘操作建议（能追/回避） |
| 上午盘中 | `intraday_am` | 09:30–11:30 | 上午分时（截至当前）、实时板块涨幅/资金、实时涨停/炸板、昨日涨停今日表现、早盘前决策/竞价复盘对照 | 上午盘面回顾 + 午后走势判断 + 操作计划 |
| 午间复盘 | `noon` | 11:30–13:00 | 上午分时全量、板块涨幅/资金、涨停/炸板、上午复盘对照 | 上午回顾 + 下午走势判断 + 买卖计划 |
| 下午盘中 | `intraday_pm` | 13:00–15:00 | 全天分时（截至当前）、板块资金流、涨停梯队/炸板、情绪指标、午间复盘对照 | 下午走势判断 + 尾盘策略 + 明日预案 |
| 收盘复盘 | `close` | 15:00 后（**默认**） | 全天行情：指数/涨跌停/板块/情绪/资金/龙虎榜/资讯 | 当日复盘报告 + 明日计划 |

规则：
1. **时间可用性判定**（服务端判定 + 前端提示）：所选时间点不在模式窗口内 → 中文提示并建议
   最近可用模式（如 09:40 选 auction → 提示「竞价数据已结束，建议切换上午盘中或午间复盘」；
   09:25–09:30 → 建议 auction/intraday_am；非交易日选盘中模式 → 提示非交易日）；
2. **历史日期补做**：允许任意日期补做任意模式；数据源无历史 → 对应数据块标注
   「数据缺失（数据源保留期外/仅当日）」并继续流程；
3. **同日多份**：同一日期可多次复盘，每次独立 `HHMMSS` 目录；
4. 盘中模式执行建议：`intraday_am` 建议 10:00 后、`intraday_pm` 建议 14:00 前（可选，不强制）。

## 三、架构

```
backend/main.py                 # FastAPI：jobs / reviews / chats / 静态托管
src/stock_review_crew/
  config.py                     # LLM + TUSHARE_TOKEN + KAIPANLA_COOKIE + TDX_PATH 只读 .env
  state.py                      # ReviewState + ChatState + 模式字段
  graph.py                      # 6 模式感知图 + 进度钩子 + 降级
  prompts.py                    # 6 模式提示词模板 + 主持人/复盘助手角色
  chat.py                       # 聊天引擎（会话/交叉问答/免责声明强制）
  storage/reviews.py            # data/reviews 读写删 + 上下文注入
  storage/chats.py              # data/chats 会话持久化
  tools/stock_data.py           # 重写：超时/降级链/模式化取数/竞价/分时/龙虎榜
  tools/tdx_local.py            # 新增：通达信本地数据（F:\tdx vipdoc）
  knowledge_store.py            # 保留 + chat_history 集合
  skills/__init__.py            # 保留
skills/                         # 5 份 skill.json 保留 + 新增复盘助手角色
frontend/                       # React + Vite + ECharts（从 synalysis 迁移改造）
data/reviews/  data/chats/  data_cache/  chroma_db/
tests/  scripts/  docs/
```

### 复盘流水线
`POST /api/reviews {date, mode, max_rounds?} → {job_id}`
→ 模式化取数（每源 ≤30s 超时、降级链、快照）→ 资讯分析 → 趋势派（并行）→ 情绪派（并行）
→ 主持人 → 辩论（≤3 轮）→ 报告（强制免责声明）→ 落盘 `data/reviews/{date}/{HHMMSS}/`
前端轮询 `GET /api/jobs/{job_id}`（阶段：取数/资讯/趋势派 n/2/情绪派 n/3/主持人/辩论/报告/完成）。

### 聊天流水线
`POST /api/chat/sessions {target_type, target, analysts[], title?} → {session_id}`
`POST /api/chat/sessions/{id}/messages {content} → {messages[], disclaimer}`
上下文 = skill 人格 + 标的实时/历史数据 + chroma 相关复盘结论 + 历史消息；
交叉问答 = 逐位分析师对上一轮回答表态后汇总；每条回复强制追加免责声明。

## 四、数据源与降级链

### 4.1 数据源优先级（Q5 确认）
| 数据项 | 主源 | 备用 | 历史深度 | 备注 |
|---|---|---|---|---|
| 指数日线/均线 | 通达信 akshare | 通达信本地 `F:\tdx\vipdoc\sh\lday` | 长 | 本地可离线 |
| 涨停/跌停池、连板梯队 | 东财 | 同花顺/Tushare 计算 | 约 30 交易日 | 超期标注缺失 |
| 板块涨幅/资金 | 同花顺 | 东财板块 | 实时+历史 | 历史逐行业慢，缓存 |
| 个股日线/情绪 | Tushare | 东财/通达信本地 | 长 | token 移入 .env |
| 竞价数据 | **开盘啦（Cookie）** | 同花顺竞价页 → 东财分时 09:25 推算 | 当天/近数日 | Cookie 未提供前走降级 |
| 分时/分钟线 | 东财 | 通达信本地（minline） | 待实测 | 超期标注缺失 |
| 龙虎榜 | 东财 | Tushare top_list | 长 | 新增 |
| 隔夜外盘（纳指/A50） | 新浪/东财外盘 | — | 仅当天 | 历史补做标注缺失 |
| 财经资讯 | 财新（当天）/央视/经济日历 | — | 财新仅当天 | 同上 |
| 全市场涨跌分布/炸板率 | Tushare 计算 | 东财 | 长 | — |

> 北向资金实时数据 2024-08 起停发，**不作为指标**（以涨跌家数/昨日涨停表现/炸板率替代）。

### 4.2 降级链约定
`主源 → 备用源 → 本地缓存 → 明确标注「数据缺失/估算」`；每个数据块携带 `source` 与
`degraded` 标记；任何降级必须进入报告/快照可见标注区，禁止静默给错数据。

### 4.3 所需凭据（均只入 `.env`，不进 git）
- `DEEPSEEK_API_KEY` / `DEEPSEEK_BASE_URL`（已有）；
- `TUSHARE_TOKEN`（从 config.py 硬编码移入，Q2）；
- `KAIPANLA_COOKIE`（Q1，用户提供后填入；未填走降级）；
- `TDX_PATH=F:\tdx`（Q3）；
- 代理 `127.0.0.1:7890`（Q4，git push/uv add/外网 API）。

### 4.4 开盘啦 Cookie 提供方法（Q1）
1. 用 Chrome/Edge 打开 https://www.kaipanla.com 并登录账号；
2. F12 → Network 面板 → 刷新页面 → 任选一个 kaipanla.com 的请求（如 `Index/index`）；
3. 复制该请求 Headers 里的完整 `Cookie:` 值；
4. 粘贴到 `H:\stock_review_crew\.env`：`KAIPANLA_COOKIE=粘贴值`（含引号则去掉引号）；
5. Cookie 可能过期，失效后重抓即可；数据层每次请求自动带该 Cookie 并做失败降级。

## 五、API 契约（前端依赖，冻结）

> 约定：比率一律**小数**（0.5=50%）；金额单位元；日期 `YYYY-MM-DD`；时间点 `HHMMSS`；
> `None` = 无数据（前端显示「—」，禁止 0）；前端负责全部格式化（×100/中文）；
> 所有投资相关内容响应恒含 `disclaimer` 字段（后端程序级强制）。

```
GET  /api/health                        → {status:"ok"}
POST /api/reviews       Body:{date, mode, max_rounds?} → {job_id}
GET  /api/jobs/{job_id} → {job_id, status(queued|running|done|error), stage, pct, message,
     analysts_done, analysts_total, result|null, error|null}
     result = {record_id, meta, report, snapshot}
     meta = {date, mode, mode_label, time, created_at, degraded[], disclaimer, summary}
GET  /api/reviews       → [{date, items:[{record_id, mode, mode_label, time, created_at, summary}]}]
GET  /api/reviews/{date}/{time} → {meta, report, analyses[], debate_history[], snapshot}
DELETE /api/reviews/{date}/{time} → 204 / 404（中文）
GET  /api/reviews/context?date=&mode= → {yesterday, earlier_today[]}
POST /api/chat/sessions Body:{target_type:stock|sector, target, analysts:[...], title?} → {session_id}
POST /api/chat/sessions/{id}/messages Body:{content} → {session_id, messages[], disclaimer}
GET  /api/chat/sessions?target=&date=  → 会话列表
GET  /api/chat/sessions/{id}           → {meta, messages[], disclaimer}
DELETE /api/chat/sessions/{id}         → 204 / 404（中文）
```

前端 normalize 层严格对照本契约，配契约断言脚本（参考 synalysis `verify-normalize.mjs`）。

## 六、数据字典约定（口径铁律）

1. 比率一律小数存储；上游百分比数值（如 `pct_change=9.98`）进契约前 `/100`，
   快照 raw 保留原值并标注单位；
2. None = 无数据，前端「—」禁 0；非有限数输出 None；
3. 红涨绿跌：正红 `#E03131`、负绿 `#0CA678`、零中性灰；警示/免责声明框不参与；
4. 中文展示：月份「2025年11月」、日期「2025-11-27」、状态/来源中文；
5. 情绪指标口径（写死）：昨日涨停今日平均收益/红盘率/连板率/核按钮率；
   炸板率 = 炸板数 ÷ 摸过涨停数；涨跌家数比；涨停按板块差异化
   （主板 10%、创业板/科创板 20%），过滤 ST/北证；
6. 竞价指标口径（数据源确认后冻结）：高开幅度 = (竞价参考价 − 昨收) ÷ 昨收；
   竞价金额 = 9:25 集合竞价成交额；抢筹/砸盘 = 9:20–9:25 委托净流入方向；
   异动榜 = 竞价涨跌幅/金额排名前列；
7. 免责声明 `"仅供参考，不构成投资建议"`：报告与聊天回复在组装层强制追加，
   响应恒含 `disclaimer` 字段；前端固定横幅；
8. 降级链：主源→备用→缓存→标注缺失；LLM 无 Key→规则引擎（degraded=True 可见）；
   历史缺失明确说明，禁止编造；
9. 边界：每源超时 ≤30s、LLM 超时 120s、任务总时长上限、磁盘写入失败不阻塞主流程。

## 七、任务分配与文件所有权（并发 ≤5）

| Issue | 模块 | 唯一写权限（草案） | 只读契约 |
|---|---|---|---|
| I1 | 数据层：模式化取数/超时/降级链/竞价/分时/龙虎榜/通达信本地/开盘啦 Cookie | `src/stock_review_crew/tools/**`、`tests/test_data*.py` | requirements.md §二/§四/§六 |
| I2 | 复盘引擎：6 模式 LangGraph/提示词/主持人/助手角色/进度钩子/降级 | `src/stock_review_crew/{graph,state,prompts}.py`、`skills/**`、`tests/test_graph*.py` | §二/§六 + I1 工具契约 |
| I3 | 聊天引擎：会话/交叉问答/检索/免责声明 | `src/stock_review_crew/chat.py`、`storage/chats.py`、`tests/test_chat*.py` | §五 + skills |
| I4 | 历史存储：reviews 读写删/上下文注入/旧 output 导入脚本 | `storage/reviews.py`、`scripts/import_output_history.py`、`tests/test_storage*.py` | §一.5/§五 |
| I5 | 后端 API：jobs/reviews/chats/静态托管/中文错误 | `backend/**`、`tests/test_backend*.py` | §五 + I1-I4 契约 |
| I6 | 前端：React/Vite/ECharts/Codex 风格/进度/聊天/历史分组/红涨绿跌 | `frontend/**`（npm 依赖可装） | §五（开发期 mock 对齐） |
| R1（Wave 3） | 全量业务逻辑审计 | 只读，输出审计报告 | 全部代码 + 本文档 |

规则：禁止 git、禁止改他人文件、禁止真实数据入库、`python -m pytest` 用 `.venv` Python
（前端 `npm run build` 验证）；发现问题写入报告由主 agent 派发修复；模块负责人完成后
**保留待命**，登记表写入 `docs/PROGRESS.md`，后续修改 resume 复用。

## 八、流程
1. ✅ 需求确认（本文档）
2. 创建 GitHub issues（I1-I6 + R1，含验收标准与所有权表）→ 首提基线（补 .gitignore）
3. Wave 1：I1 + I2 + I3 + I4 + I6 并行（5）
4. Wave 2：I5 后端集成 + 主 agent 集成审查（全量 pytest、npm build、启动冒烟）
5. Wave 3：R1 只读审计 → 派发修复
6. 冒烟：6 模式真实数据各至少 1 次（今日午间窗口 noon；其余近期交易日补做，
   盘中模式在窗口内执行或标注降级）→ 验收交付（PROGRESS.md、README、提交推送）

## 九、风险（承接草案 §十，增量）
- R1 竞价数据依赖开盘啦 Cookie（未提供前降级实现）；R2 东财涨停池 30 交易日限制；
  R3 分时历史深度待实测；R5 token 迁移后须验证可用；R6 代理需启动客户端后验证；
  R11 通达信本地文件格式解析（.day/.lc1/.lc5）工作量可控但需隔离测试（只读、不写回）；
  R12 6 模式提示词与冒烟工作量增大，冒烟按窗口灵活排期。
