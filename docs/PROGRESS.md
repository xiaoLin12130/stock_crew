# 项目进度存档（2026-08-03 11:50 需求确认）

## 状态
- ✅ 需求确认：`docs/requirements.md`（v1，Q1-Q10 全部确认，6 模式）
- ✅ 环境核查：Python 3.12.11（.venv）；Node 22；F:\tdx 通达信本地数据可用；
  gh CLI 未装；**网络根因已定位**：沙箱注入假代理 `http://127.0.0.1:9`，
  显式指定 `http://127.0.0.1:7890` 后 git/python 均可联网（PLAYBOOK §1）
- ✅ GitHub：仓库已验证（public / main / LICENSE 2711d1f）；基线已推送
  （合并提交 `222bacb`）；**7 个 issue 已创建 #6-#12**
  （#6 I1 / #7 I2 / #8 I3 / #9 I4 / #10 I5 / #11 I6 / #12 R1）
- ✅ Wave 1 全部完成：I1 数据层（67 tests）+ I2 复盘引擎（32）+ I3 聊天引擎（25）
  + I4 历史存储（50）+ I6 前端（build/normalize/冒烟通过）
- ✅ Wave 2 完成：I5 后端（16 tests）+ 集成修复（全量 **190 passed**，进程干净退出）
- ✅ 集成修复：py_mini_racer 禁用（Windows IOCP 退出死锁）、posthog 遥测禁用、
  chroma 降级兜底（PanicException 捕获）、东财 HTTPS→HTTP 改写+多子域轮换、
  config 兼容 DEEPSEEK_BASE_URL、.env 修复（BOM/拼接残留/换新 Tushare token/
  TDX_PATH）、.venv 补齐 fastapi/pytest（uv sync）
- ✅ 真实数据冒烟：6 模式 + 聊天 + 窗口校验（详见 docs/SMOKE.md）
- ✅ Wave 3：R1 业务审计完成（docs/audit-report.md；S1-1 聊天白名单、S2-1 涨跌比展示、
  M1-2/M1-3/M1-4/M3 全部修复；复测 190 passed）
- ✅ 交付：git 历史重写为单提交 `7953229`（旧 token 已从历史移除）并推送
  stock_crew.git（main）；Cloudflare Tunnel 已部署（scripts/start_tunnel.ps1）
- ⏳ Wave 2：I5 后端 API + 集成审查
- ⏳ Wave 3：R1 业务逻辑审计 → 派发修复
- ⏳ 冒烟：6 模式真实数据（2026-08-03 午间窗口 noon；其余近期交易日补做）
- ⏳ 验收：全量 pytest、npm build、README、提交推送 stock_crew.git

## 关键文件
- 需求契约：docs/requirements.md（唯一真源）
- 草案存档：docs/requirements-draft.md；issue 文本：docs/issues.md

## Agent 负责人登记（模块 → agent，后续修改优先 resume 原负责人）
| 模块 | 负责人（昵称/id） | 状态 |
|---|---|---|
| I1 数据层 tools | Wegener（019fc5c7-f7c1-7133-aa84-710ee6b3493c） | ✅ 完成待命（67 tests；6 模式/降级链/竞价三源/tdx_local 只读/双接口） |
| I2 复盘引擎 graph/prompts | Laplace（019fc5c7-f871-7ba3-a157-90a59c72e280） | ✅ 完成待命（32 tests；6 模式感知/降级/进度钩子/免责声明；新增 host+review_assistant 角色） |
| I3 聊天引擎 chat/chats-storage | Arendt（019fc5c7-f95c-7743-8cfa-61d4b76161dc） | ✅ 完成待命（25 tests；单/多分析师交叉/免责声明/chroma chat_history） |
| I4 历史存储 reviews/导入脚本 | Lorentz（019fc5c7-fa62-7130-9b02-6688f5777258） | ✅ 完成待命（50 tests：存储 28 + 导入 22；真实 output/ 导入冒烟通过） |
| I5 后端 API | 待指派（Wave 2） | 待命 |
| I6 前端 frontend | Bernoulli（019fc5c7-fb4a-7ff2-bab5-d53910475a49） | ✅ 完成待命（build 通过；normalize 断言通过；headless 冒烟通过，快照子结构已固化供 I5 联调） |
| R1 业务审计 | 待指派（Wave 3，只读） | 待命 |

> 策略：模块负责人任务完成后不立即关闭；同类修改优先 resume；仅当确定不再改动或
> 并发名额不足时才回收。

## 冒烟计划（6 模式，每条记录日期/模式/数据源与降级/输出摘要）
| 模式 | 计划日期 | 说明 |
|---|---|---|
| noon | 2026-08-03（今日 11:30-13:00 窗口） | 实时数据 |
| pre_market | 近期交易日补做（如 2026-07-31） | 外盘/资讯标注仅当日缺失 |
| auction | 近期交易日补做 | 竞价数据按降级链 |
| intraday_am / intraday_pm | 窗口内执行或补做标注 | 分时历史深度实测 |
| close | 2026-08-03 15:00 后 | 实时数据 |

## 阻塞项
- ✅ 已解决：GitHub 连接器 403 → 用 PAT 走 api.github.com（python urllib + 代理）建 issue 成功
- ✅ 已解决：远端仓库确认存在（public），基线已推送（222bacb）
- ✅ 已解决：网络 → 显式代理 127.0.0.1:7890（沙箱假代理 127.0.0.1:9 需覆盖）
- ⏳ Wave 1 进行中：I1 数据层 / I2 复盘引擎 / I3 聊天引擎 / I6 前端（4 个运行中）
- ⏳ 遗留：仓库根目录存在 agent 测试产生的临时目录（pt_*/sdc_*/sr_*/tmp* 等），集成阶段清理
- ✅ 遗留已清理（探针目录已删；剩余已知环境性降级见 docs/SMOKE.md「环境性降级说明」）
