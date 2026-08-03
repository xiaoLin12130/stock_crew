# R1 业务逻辑审计报告（Wave 3，只读）

- 审计时间：2026-08-03（Asia/Shanghai）
- 审计范围：docs/requirements.md（唯一契约）逐项对照代码与测试
- 审计对象：backend/main.py、src/stock_review_crew/**（graph/chat/state/prompts/
  knowledge_store/storage/tools）、frontend/src/**、scripts/import_output_history.py、tests/**
- 方式：只读代码审查 + 全量离线测试复跑（`python -m pytest -p no:cacheprovider tests/`）
- 测试结果：**189 passed / 1 failed**（失败为环境性，见 M3）

严重度定义：S1=契约破坏/功能不可用；S2=明显缺陷需修复；M=次要问题/健壮性建议。

---

## 一、问题清单（按模块 owner 归类）

### 集成（前端 ↔ 后端）

#### S1-1 前端聊天分析师选择与后端白名单完全脱节，真实后端下聊天会话创建必失败

- 现象：前端 [HomeView.jsx](/H:/stock_review_crew/frontend/src/components/HomeView.jsx:65)
  把 mock 分析师显示名（如「趋势派·老周」）包装成 `analysts: [{skill_name: ...}]` 提交；
  后端 [chat.py](/H:/stock_review_crew/src/stock_review_crew/chat.py:295) 按
  `get_analyst_whitelist()`（来自 [skills/__init__.py](/H:/stock_review_crew/src/stock_review_crew/skills/__init__.py:25)
  注入的目录 id：alang/bingchuan/baxiaoxian/yangjia/tiechui）做白名单校验。
  实测：前端发送 `['趋势派·老周','情绪派·阿凯']` → 后端 `unknown=['趋势派·老周','情绪派·阿凯']` → 400
  「分析师不在白名单中」。
- 根因：I6 的 mock 分析师池（[mock.js](/H:/stock_review_crew/frontend/src/mock.js:10)，
  id=trend_zhou 等 5 人）是演示数据，从未与 I3 真实 skills 对齐；前端既不发合法 id，
  也不展示真实分析师（阿狼/爱在冰川/拔小弦/炒股养家/铁锤）。
- 影响：production 模式（非 DEV/非 offline）聊天卡片入口 100% 失败；
  聊天全链路（create → message）无法使用。
- 修复建议：前端 `ANALYSTS` 改为读取真实 skills（或后端新增
  `GET /api/chat/analysts` 白名单接口）；`onOpenChat` 传
  `analysts: [真实id数组]`（字符串），`normalizeChatAnalysts` 已兼容字符串数组；
  同步补一个集成测试：用前端 payload 调 `POST /api/chat/sessions` 应 200。

#### M1-2 后端分析师白名单泄漏主持人/复盘助手角色

- 现象：[chat.py](/H:/stock_review_crew/src/stock_review_crew/chat.py:43) 的
  `get_analyst_whitelist()` 直接列全部 skills 目录 id，含 `host`、`review_assistant`，
  实测白名单 = `['alang','baxiaoxian','bingchuan','host','review_assistant','tiechui','yangjia']`。
- 影响：用户可创建「主持人」「复盘助手」会话，属于角色泄漏；S1-1 修复时应收口为 5 位分析师。
- 修复建议：白名单显式枚举 5 位分析师（或按 `group` 字段过滤），并加断言测试。

### I6 前端

#### S2-1 涨跌家数比（up_down_ratio）被当作百分比率展示

- 现象：后端快照 [main.py](/H:/stock_review_crew/backend/main.py:393) 中
  `up_down_ratio` 语义为比值（如 1.56 = 上涨 1.56 家/下跌 1 家，mock 也如此），
  但前端 [Charts.jsx](/H:/stock_review_crew/frontend/src/components/Charts.jsx:274)
  将其列入比率项，tooltip 与柱状图按 ×100 渲染成「155.56%」；
  [mock.js](/H:/stock_review_crew/frontend/src/mock.js:139) 报告生成也写
  `涨跌家数比 155.6%`。
- 影响：展示口径错误（不符合「比率一律小数」的 ×100 展示约定——该字段本非 0~1 比率）。
- 修复建议：前端单独按比值格式化（如 `1.56` 或 `1.56:1`），或后端在快照中额外提供
  归一化字段；两端口径写进 normalize 断言。

#### M1-3 pre_market/auction 模式无指数分时图数据

- 现象：[stock_data.py](/H:/stock_review_crew/src/stock_review_crew/tools/stock_data.py:2038)
  的 pre_market 只取 index_trend/macro/news，auction 只取 auction，
  均无 minute 块；[main.py](/H:/stock_review_crew/backend/main.py:276)
  `_snap_index_minute` 只能从 minute 块组装 → 这两个模式的 `index_minute` 恒为空数组，
  前端图区显示「暂无指数分时数据」。
- 影响：报告页图表缺指数分时（非数据错报，属展示缺口）。
- 修复建议：pre_market 用 index_trend 日线末段组装参考曲线（mock 已有此语义），
  auction 可不展示分时图但建议快照注明「竞价模式无分时」。

### I2 引擎

#### M1-4 规则引擎事实提取缺涨跌家数比，情绪标签判定与快照口径不完全一致

- 现象：[graph.py](/H:/stock_review_crew/src/stock_review_crew/graph.py:507)
  `_rule_facts` 未提取 sentiment 块的 `up_down_ratio`（仅 up/down 计数）；
  而 [main.py](/H:/stock_review_crew/backend/main.py:393) 快照有该字段。
- 影响：规则版报告不会引用涨跌家数比（缺失保守，不构成错报）。
- 修复建议：`_rule_facts` 补充 `up_down_ratio` 读取，与快照字段对齐。

### I3 聊天

- 已核对：单/多分析师交叉问答、逐位表态、汇总、上下文注入（skill 人格+标的数据+
  chroma 复盘结论+历史消息）、LLM 无 Key/失败降级为中文说明并附免责声明、
  `send_message` 异常不抛出（参数错误除外）、会话不存在返回 None→404，
  chroma 全部 BaseException 兜底（Rust PanicException）。无新增问题（S1-1/M1-2 属集成）。

### I4 存储 / 导入

- 已核对：`data/reviews/{date}/{HHMMSS}[/-N]` 同日多份、日期倒序+同日时间倒序、
  删除 204/404、路径穿越双重防护、损坏容错、`STOCK_REVIEW_DATA_DIR` 可覆盖；
  context() 昨日 close 优先→任意模式兜底、当日更早时间点倒序，无历史返回 None 不编造；
  chats 持久化/过滤/删除/损坏重建；导入脚本幂等（imported_from_legacy 标记跳过、
  --force 覆盖不产生 -N）、模式按时间点推断（09:25-09:30 空档归 close 并标注）、
  dry-run 不写盘。无新增问题。

### I5 后端

- 已核对：§五全部端点、轮询契约字段齐备且 `_` 内部字段不外泄、JOBS TTL>1h+保留 50
  条+进行中不删、参数校验 400/404 全中文、任何异常→status=error 中文不崩线程、
  落盘失败不阻塞返回（degraded 标注）、静态托管 frontend/dist。
- 模式窗口与前端一致性：backend [MODE_WINDOWS](/H:/stock_review_crew/backend/main.py:78)
  与 [modes.js](/H:/stock_review_crew/frontend/src/modes.js:31) 逐分钟一致
  （pre_market 0-09:15 / auction 09:15-09:25 / intraday_am 09:30-11:30 / noon 11:30-13:00 /
  intraday_pm 13:00-15:00 / close 15:00-24:00）；09:25-09:30 缓冲段双方均给定向提示；
  历史日期任意模式放行；非交易日（周末）盘中模式 400 提示一致。
  ⚠️ 注意：周末判定未覆盖法定节假日（如工作日休市），前端 `isTradingDay` 与后端
  `_window_error` 同样只判 weekend，属已知边界，契约未要求交易日历，不列为缺陷。

### I1 数据层

- 已核对：6 模式取数节点块集与契约 §二 对齐（test_data_modes 参数化断言）；
  每源超时 ≤30s（`SOURCE_TIMEOUT_SECONDS=30`，东财竞价推算 25s 总预算）；
  降级链 主源→备用→本地缓存→「数据缺失/估算」全部携带 source/degraded/degraded_reason；
  Cookie 未配置静默降级不中断；比率一律小数（%÷100、raw 保留原值+单位标注）；
  None 语义（非有限数→None，无数据不填 0）；炸板率=炸板÷摸板、涨跌家数、涨停差异化
  （主板 10%/创业科创 20%）、过滤 ST/北证均有手算测试；tdx_local 只读（open "rb"），
  目录不可写；config.py 无硬编码 token（test_data_config 断言）。
- 已知环境性降级（非代码缺陷，报告中注明）：
  - 东财网络不稳定（本机 SNI Reset）→ HTTPS→HTTP 改写+多子域轮换已实现并标注；
  - Tushare 沙箱 tk.csv 写权限受限 → 测试一律 mock，生产走降级标注；
  - 同花顺 mini_racer 依赖被禁用（Windows IOCP 退出死锁）→ 相关接口走降级标注，
    PLAYBOOK §8 已记录。

### 凭据与隐私

- 已核对：`.gitignore` 覆盖 `data/`、`data_cache/`、`output/`、`chroma_db/`、`.tmp/`、
  `.env`/`.env.local`、`node_modules/`、`frontend/dist/`；`git ls-files` 确认无任何
  `.env`/data 文件被跟踪；config.py 仅 `os.getenv` 读取，测试断言无硬编码 token；
  日志仅记录异常信息与 job_id，无真实数据落日志。

### 免责声明

- 已核对：报告组装层 [ensure_disclaimer](/H:/stock_review_crew/src/stock_review_crew/graph.py:884)
  与 `build_result` 双重强制（唯一 Markdown 末尾）；聊天 [chat.py](/H:/stock_review_crew/src/stock_review_crew/chat.py:376)
  逐条回复组装层强制拼接免责声明，所有响应恒含 disclaimer 字段；前端报告页/聊天页
  均有固定横幅；
  mock 路径同样带 disclaimer。无遗漏路径。

### 前端 ↔ 后端契约映射（normalize）

- 已核对：normalize.js 与 §五 及 backend 实际响应一致（嵌套 meta/report/analyses/
  debate_history/snapshot、record_id/session_id `{date}_{time}`（复盘可带 -N）、
  created_at 无时区 ISO `YYYY-MM-DDTHH:mm:ss`、快照子结构 index_minute/limit_ladder/
  sectors/sentiment/source/degraded 与后端 `_to_frontend_snapshot` 输出键完全一致）。
- 快照键集由 [test_backend.py](/H:/stock_review_crew/tests/test_backend.py:171)
  `assert set(snap) == {...}` 固化为契约断言。S2-1（up_down_ratio 展示）与
  M1-3（pre_market/auction 无分时）除外，无其他不一致。

### 测试覆盖

- 已核对：数据层关键指标均有手算/确定性用例（炸板率 1/3、情绪 avg_return=0.071、
  高开幅度 (1515-1500)/1500=0.01、旧缓存 %→小数 0.72→0.0072 等）；断言容差用
  `pytest.approx` 有效（相对误差），无过宽容差；全部测试离线确定性（无网络、无真实
  LLM、隔离目录）；复跑结果 **189 passed / 1 failed**。
- 缺陷：无测试覆盖「前端聊天 payload → 后端白名单」集成链路（S1-1 漏网根因）；
  `test_backend` 的假取数快照与真实 I1 blocks 键名结构一致（zt_pool 嵌套 limit_up/
  limit_down），对真实数据链路有代表性，但缺少「真实 I1 fetch_mode_data 输出 →
  _to_frontend_snapshot」的直接映射测试（建议补一条，用 test_data_modes 的 mock 源
  走 backend 快照转换）。

---

## 二、环境性/非代码事项（报告中注明，不算缺陷）

1. **东财网络不稳**：本机对 eastmoney TLS 做 SNI Reset，HTTPS→HTTP 改写+多子域轮换
   已实现；偶发 502/Reset 由降级链标注。
2. **Tushare 沙箱 tk.csv 写权限**：沙箱内 Tushare 首启写 token 文件受限，测试全 mock，
   生产由降级链标注；`.env` 已配 token。
3. **同花顺 mini_racer 禁用**：py_mini_racer 构造会触发 Windows IOCP 退出死锁，
   已在 config.py/conftest 全局替换为抛错 stub，相关 akshare 接口走降级标注。
4. **全量测试 1 失败（M3）**：`test_root_override_isolation` 断言仓库根
   `data/reviews` 不存在，但仓库已存在冒烟真实数据目录（`data/reviews/2026-07-31`、
   `data/reviews/2026-08-03`、`data/chats/...`，均为今日冒烟产物，`.gitignore` 已排除）。
   测试的隔离逻辑本身正确（隔离目录写入断言全过）；该断言对环境有前置条件，
   建议改为「断言未写入仓库」而非「目录不存在」。

---

## 三、修复优先级建议

| 编号 | 严重度 | owner | 一句话修复 |
|---|---|---|---|
| S1-1 | S1 | I6+I3（集成） | 前端分析师池/请求体与后端白名单 id 对齐，补集成测试 |
| S2-1 | S2 | I6 | up_down_ratio 按比值展示（×100 仅用于 0~1 比率） |
| M1-2 | M | I3 | 白名单收口 5 位分析师，排除 host/review_assistant |
| M1-3 | M | I6+I1 | pre_market 快照补指数参考曲线（或明确标注无分时） |
| M1-4 | M | I2 | `_rule_facts` 补 up_down_ratio 与快照对齐 |
| M3 | M | I4 测试 | 隔离断言改为「不写入仓库」，容忍冒烟产物存在 |

> 免责声明：本报告为代码与测试的契约符合性审计，不构成投资建议。
