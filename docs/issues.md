# Issue 清单（2026-08-03，需求确认版）

> 状态：GitHub 连接器对 `xiaoLin12130/stock_crew` 无访问权限（403 Resource not
> accessible），issue 全文存档于此；授权后按编号补建（标题/正文可直接复用）。
> 关联契约：docs/requirements.md（唯一真源）。

---

## I1 数据层：模式化取数 + 超时/降级链 + 竞价/分时/龙虎榜 + 通达信本地 + 开盘啦 Cookie

**任务**
1. 重写 `tools/stock_data.py`：6 模式化取数节点（pre_market/auction/intraday_am/noon/
   intraday_pm/close），每源超时 ≤30s，主源→备用源→本地缓存→标注「数据缺失/估算」降级链，
   每块数据携带 source/degraded 标记。
2. 竞价数据：优先读 `KAIPANLA_COOKIE`（.env）请求开盘啦；未配置/失败→同花顺竞价→
   东财分时 09:25 竞价柱推算；全部失败标注缺失，绝不抛给前端。
3. 新增分时/分钟线（东财为主、通达信本地备用）与龙虎榜（东财→Tushare top_list）。
4. 新增 `tools/tdx_local.py`：只读解析 `F:\tdx\vipdoc`（lday 日线 / minline 分时），
   禁止写回通达信目录。
5. 情绪指标口径照契约 §六.5（炸板率=炸板÷摸板、主板10%/创业科创20%、过滤ST/北证）。
6. 比率一律 /100 为小数进契约；快照 raw 保留原值标注单位。
7. `config.py` 删除硬编码 Tushare token，只读 .env。

**验收**：每模式取数节点真实可用 OR 明确降级标注（断言 source/degraded）；离线降级链测试；
通达信本地解析只读单元测试；无 Cookie 时竞价返回「数据缺失」且流程不中断。

**所有权**：写 `tools/**`、`config.py`、`tests/test_data*.py`；只读 requirements.md、data_cache/。

---

## I2 复盘引擎：6 模式感知 LangGraph + 提示词 + 主持人/复盘助手 + 进度钩子 + 降级

**任务**
1. 重构 `graph.py` 模式感知：fetch 按 mode 分发取数，news→trend(并行)→sentiment(并行)→
   host→debate(≤3)→report 复用。
2. `prompts.py`：6 模式提示词模板（含「昨日/更早复盘对照」区块）+ 主持人/复盘助手角色。
3. 进度钩子：取数/资讯/趋势派 n/2/情绪派 n/3/主持人/辩论/报告/完成。
4. 上下文注入：昨日收盘 + 当日更早时间点（I4 契约）；缺失时「无昨日报告，跳过验证」。
5. 降级：LLM 无 Key/失败→规则引擎（degraded=True）；结构对齐 §五。
6. 免责声明程序级强制；skills 只允许新增复盘助手 JSON，不改分析师人格。

**验收**：6 模式无 Key 降级链路测试；进度阶段序列测试；昨日报告缺失断言；模板注入测试。

**所有权**：写 `{graph,state,prompts}.py`、`skills/**`（仅新增）、`tests/test_graph*.py`；
只读 requirements.md、tools/**（契约）、skills/__init__.py。

---

## I3 聊天引擎：会话/单分析师/多分析师交叉/历史检索/免责声明强制

**任务**
1. `chat.py`：会话（target_type=stock|sector、analysts[]）；上下文 = skill 人格 +
   标的数据（I1）+ chroma 复盘结论 + 历史消息。
2. 多分析师交叉：逐位对上一轮表态（同意/反对/补充）后汇总；串行，LLM 超时 120s。
3. `storage/chats.py`：`data/chats/{date}/{HHMMSS}/`；按标的/日期过滤；删除 204/404。
4. chroma `chat_history` 集合（或扩展 review_history）。
5. 免责声明强制；无 Key 降级为中文说明 + 免责声明，不抛异常。

**验收**：单分析师 mock 测试（上下文断言）；交叉测试（≥2 分析师含逐位片段与汇总）；
持久化往返（创建→消息→过滤→详情→删除）；免责声明恒有。

**所有权**：写 `chat.py`、`storage/chats.py`、`tests/test_chat*.py`；
只读 requirements.md、skills/**、knowledge_store.py、tools/**。

---

## I4 历史存储：reviews 读写删/上下文注入/旧 output 导入脚本

**任务**
1. `storage/reviews.py`：`data/reviews/{date}/{HHMMSS}/{meta,report,snapshot}.json`；
   同日多份；按日期分组倒序列表；详情；删除；id 安全校验；损坏容错；数据根目录环境变量可覆盖。
2. `context(date, mode)` → {yesterday, earlier_today[]}。
3. `scripts/import_output_history.py`：`output/{date}/复盘_{HHMMSS}.md` 幂等导入
   （模式按时间点推断），快照标注 imported_from_legacy。
4. 测试一律走环境变量隔离目录。

**验收**：同日多份/列表/详情/删除测试；上下文注入（含无历史）；导入幂等重跑；路径穿越拒绝。

**所有权**：写 `storage/reviews.py`、`scripts/import_output_history.py`、`tests/test_storage*.py`；
只读 requirements.md、output/、knowledge_store.py。

---

## I5 后端 API：FastAPI jobs/reviews/chats/静态托管/中文错误 + 集成测试

**任务**
1. `backend/main.py`：§五 全部端点；模式窗口中文提示；JOBS TTL（>1h 移除、保留 50）；
   异常→status=error + 中文 message；静态托管 frontend/dist。
2. 异步：线程 + 进度回调；上传/参数校验 400/413/404 中文。
3. 集成测试：TestClient 全链路（复盘→轮询→done→分组列表→详情→删除→404；
   聊天会话→消息→免责声明→删除），隔离存储目录，无 Key 规则引擎降级。

**验收**：test_backend 全绿离线确定性；错误路径中文断言；窗口判定（09:40 选 auction→提示）；
轮询契约字段齐备、内部字段不外泄。

**所有权**：写 `backend/**`、`tests/test_backend*.py`；只读 requirements.md、src/**（I1-I4 契约）。

---

## I6 前端：React/Vite/ECharts Codex 风格（复盘/进度/报告/聊天/历史分组/红涨绿跌）

**任务**
1. 从 synalysis_crew/frontend 迁移改造；深色侧栏 #17181D、内容区 #F7F7F5、强调绿
   #10A37F、全中文；侧栏收起/展开（brand=恢复入口）；「新建复盘」唯一主按钮。
2. 首页：复盘表单（日期+6 模式，窗口外中文提示）+ 聊天卡片 + 历史分组。
3. 进度条（取数/资讯/趋势派 n/2/情绪派 n/3/主持人/辩论/报告/完成）；轮询 1s；
   fetch 30s 超时中文错误；空态/加载/错误态全覆盖。
4. 报告页：Markdown + 分析师折叠卡 + 辩论 + 图表（ECharts 中文 tooltip）+ 免责声明 +
   降级标注。聊天页：会话列表 + 消息流（逐位片段+汇总）+ 免责声明横幅。
5. 红涨绿跌（#E03131/#0CA678/灰）；None→「—」；比率 ×100；normalize 契约断言脚本。
6. 历史：按日期分组、同日多份「HH:MM + 模式标签」、删除二次确认、运行中任务保留。

**验收**：npm run build 成功；normalize 断言通过（mock 对齐后端 Schema）；交互冒烟截图。

**所有权**：写 `frontend/**`；只读 requirements.md、synalysis_crew/frontend（模板参考）。

---

## R1 业务逻辑审计（Wave 3，只读）

**范围**：数据字典对照（比率/None/单位/边界）、降级链真实性、免责声明无遗漏、6 模式窗口
判定、存储/导入幂等、前端↔后端契约映射、测试手算用例与容差、凭据与隐私（.env/gitignore/日志）。

**输出**：`docs/audit-report.md`（问题清单 S1../M1.. 按 owner 归类）。

**所有权**：写 `docs/audit-report.md`；只读全部代码与文档。
