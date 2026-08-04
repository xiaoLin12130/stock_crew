# PLAYBOOK — 项目踩坑与解法手册

> 所有 agent（Codex/Claude）开工前必读。遇到环境/工具/权限/网络/业务逻辑问题时**先查这里**；
> 解决问题后**按文末模板追加新条目**，方便后续所有 agent。
> 来源：继承 synalysis_crew 的 PLAYBOOK（2026-08-03），并按本项目适配。

## 1. 网络与代理（Windows 本机）
| 问题 | 现象 | 原因 | 解决 |
|---|---|---|---|
| github.com 直连不通 | git push/curl 443 超时 | 本机网络对 github.com 不稳定 | 走代理：`git -c http.proxy=http://127.0.0.1:7890 push`（或写入仓库级 config）；curl 加 `-x http://127.0.0.1:7890` |
| api.github.com 可用 | 与 github.com 不通但 api 通 | 网络分层 | GitHub API 操作（issue/文件）可用 urllib/curl 直连 api.github.com |
| 代理客户端未启动 | 7890 端口无监听，直连/代理全部 000 | Clash 等未运行 | **请用户启动代理客户端**；启动后 `curl -x http://127.0.0.1:7890 https://api.github.com` 验证 |
| 下载 GitHub releases 失败 | curl 直连超时 | 同上 | 加 `-x http://127.0.0.1:7890` 重试 |
| **沙箱注入假代理**（本项目实测） | curl/PowerShell/git 全部连不上，`$env:HTTP_PROXY=http://127.0.0.1:9` | Codex 沙箱用假代理隔离网络 | 显式覆盖：git 用 `-c http.proxy=http://127.0.0.1:7890`；Python 用 `urllib.request.ProxyHandler({'https':'http://127.0.0.1:7890'})`；命令开头 `$env:HTTP_PROXY='http://127.0.0.1:7890'` |

## 2. GitHub Issue / 仓库操作
| 问题 | 现象 | 原因 | 解决 |
|---|---|---|---|
| 插件建 issue 404 | `Resource not found` | GitHub 插件 App 看不到你的私有仓库 | 仓库改 public，或把插件账号加为协作者（用户手动） |
| 插件建 issue 403 | `Resource not accessible by integration` | App 未安装/未授权到该账号仓库 | 插件无解 → 改用**用户的 fine-grained PAT**（仓库 + Issues 读写）走 api.github.com；网络不稳要加重试（3 次 + 退避） |
| 创建仓库 | 插件无建仓工具 | 能力缺失 | 用户手动创建，或用 PAT `POST /user/repos` 创建（private/public 均可） |
| 远端仓库未创建 | GET /repos 404 | 用户消息发错线程未真正建仓 | 用 PAT 创建或用户手动创建；创建后 `git remote add origin` + push |

## 3. 沙箱与提权（workspace-write 环境）
| 问题 | 现象 | 原因 | 解决 |
|---|---|---|---|
| git 写操作被拒 | `index.lock: Permission denied` / config 锁失败 | `.git` 目录在沙箱只读 | git 命令（add/commit/push）用 `require_escalated`，可申请 prefix `["git"]` |
| 前端构建失败 | esbuild `EPERM` spawn 子进程 | 沙箱拦截 | `npm run build` 用 `require_escalated` |
| pip/npm install 失败 | 网络/权限 | 沙箱网络受限 | 提权安装；依赖尽量一次性装好，agent 禁止自行安装 |
| 后台服务 | 需要常驻进程 | — | `Start-Process -WindowStyle Hidden`；健康检查用 `Invoke-RestMethod http://127.0.0.1:PORT/api/health` |
| pytest tmp_path 报错 | WinError 5 拒绝访问 | Windows 沙箱下 pytest 0o700 目录 ACL 问题 | 测试模块覆盖 tmp_path fixture，用 0o777 显式创建并自清理 |
| PowerShell 管道中文乱码 | python 收到 `??` | PS 按 GBK 编码管道 | 脚本用纯 ASCII（中文用 `\uXXXX`），或 `$env:PYTHONIOENCODING='utf-8'` |

## 4. 业务逻辑纪律（血泪教训）
1. **数据字典先行**：每个字段写死定义（公式/来源/边界/单位/None 语义），见 `docs/requirements.md` §六；代码与前端严格对照。
2. **口径先和用户确认再实现**（synalysis 教训）：收益率 TWR、胜率按完整交易闭环、翻倍禁止低点口径、腰斩递进计数、亏损榜升序、最大回撤正值。
3. **前端↔后端契约写进文档**：接口返回结构（含嵌套字段）完整记录，前端 normalize 层对照实现——字段不匹配会导致整页失效。
4. 比率一律小数存储（0.5=50%），前端负责 ×100；`None` 显示「—」不显示 0。
5. 长任务必须：后台 + 进度轮询 + fetch 超时（30s）+ 中文错误；控件支持重选同一文件。
6. 降级链：数据源失败 / LLM 无 Key / 历史缺失 → 明确降级并标注，绝不静默错报。
7. 测试：每个指标至少一个**手算抽查用例**；断言容差不可过大；真实数据冒烟。
8. 免责声明程序级强制追加；红涨绿跌（正红负绿）；全中文展示。

## 5. Agent 协作
- spawn_agent 并发 ≤5；每个 agent 给**文件所有权表**（只写自己名下文件）与只读契约；禁止 git/真实数据入库/改他人文件。
- 完成通知会自动推送；可用 wait_agent 主动查询状态。
- **空闲 agent 会被系统回收**（"保留待命"不等于永存）：重要任务开始前先确认负责人还在，不在就新开并把上下文写全。
- 模块负责人登记表在 `docs/PROGRESS.md`。
- 测试隔离：`STOCK_REVIEW_DATA_DIR` 指向 `.tmp/`，不要写仓库 `data/`。

## 6. 部署（可选 Cloudflare Tunnel）
- cloudflared 下载：GitHub releases（走代理 127.0.0.1:7890），放 `.tmp\cloudflared.exe`。
- 快速隧道：`cloudflared tunnel --url http://127.0.0.1:8501 --no-autoupdate`；一键脚本 `scripts\start_tunnel.ps1`。
- 快速隧道地址每次重启变化；电脑关机服务停止；公网 URL 不要公开分享。

## 7. 新问题记录模板
```markdown
## [YYYY-MM-DD] 问题一句话
- 现象：……
- 原因：……
- 解决：……
- 备注/复现：……
```

## 8. 本项目已记录问题
### [2026-08-03] GitHub 建 issue 403（本项目）
- 现象：GitHub 连接器 create_issue 全部 403 `Resource not accessible by integration`。
- 原因：GitHub App 未授权到新仓库 xiaoLin12130/stock_crew。
- 解决：用户已提供 fine-grained PAT（仓库 + Issues 读写）；待网络可用后走
  `POST https://api.github.com/repos/xiaoLin12130/stock_crew/issues` 补建（载荷在 .tmp/gh/）。

### [2026-08-03] 远端仓库可能未创建
- 现象：用户消息「仓库已经建好」发错到 synalysis 线程且被「无视」；本地 git 无 remote。
- 解决：网络恢复后先用 PAT `GET /repos/xiaoLin12130/stock_crew` 验证；404 则
  `POST /user/repos` 创建（默认 private）或请用户手动创建。

### [2026-08-03] 代理客户端未启动
- 现象：127.0.0.1:7890 无监听，直连与代理全部 000。
- 解决：请用户启动 Clash 等代理客户端；系统代理已指向 127.0.0.1:7890。

### [2026-08-03] pytest 全绿但进程退出挂起（约 2 分钟）
- 现象：190 项测试全过，解释器在退出阶段死锁，主线程卡 `threading._shutdown`。
- 原因：akshare 依赖 py_mini_racer（内嵌 V8 JS 引擎），其后台 asyncio 事件循环线程
  在 Windows 上用 IOCP 完成端口阻塞，与解释器退出形成死锁（构造 MiniRacer 本身
  就可能永久阻塞）；posthog 遥测线程的 atexit join 是次要因素。
- 解决：在 `config.py` 与 `tests/conftest.py` 导入 akshare 之前把
  `py_mini_racer.MiniRacer` 替换为抛错 stub（本项目数据链路不需要 JS 引擎），
  并在 conftest 里 `posthog.disabled = True`；数据层遇相关接口自动降级标注。

### [2026-08-03] PowerShell 空字符串赋环境变量会删除变量
- 现象：`$env:DEEPSEEK_API_KEY=''` 后配置层仍读到 .env 的真实 Key（测试走真实 LLM）。
- 原因：PowerShell 空串赋值 = 删除该环境变量，而非置空。
- 解决：在 Python 内 `os.environ['DEEPSEEK_API_KEY']=''` 或用 `Remove-Item Env:`。

### [2026-08-04] 同花顺页面 JS 签名（v Cookie）与浏览器爬虫
- 现象：q.10jqka.com.cn 行业板块页无签名请求返回 401；akshare 依赖 py_mini_racer
  执行 ths.js（Windows IOCP 退出死锁，已禁用）。
- 解决（**浏览器解析 HTML 方案**）：Playwright（.venv 已装）驱动系统 Edge 无头，
  打开页面让 JS 自动生成 v Cookie；再**页面内 fetch**（同源 + `X-Requested-With:
  XMLHttpRequest` 头）取 ajax HTML 表格（独立 ctx.request 会 403，必须页面内 fetch）。
  实现：`src/stock_review_crew/tools/browser_crawler.py`，已接入板块降级链
  （同花顺 → 同花顺(浏览器) → 东财 → 缓存）。
- 备注：playwright 仅装在 .venv（系统 Python 未装，模块内 available() 判定降级）；
  浏览器启动约 2-4s，仅作备用源；沙箱内运行需提权。

### [2026-08-04] bat 启动脚本必须 CRLF 换行
- 现象：apply_patch 生成的 .bat（LF 换行）在 cmd 下解析错乱
  （echo 被拆行、中文行报「不是内部或外部命令」）。
- 解决：批处理文件写完后统一转 CRLF（PowerShell/脚本处理），并实测 `cmd /c` 运行。

### [2026-08-04] dist 不提交 git → 部署可能用陈旧前端
- 现象：源码/提交均有「股票名称搜索」，但线上 bundle 缺失（grep 不到
  api/data/search），用户功能「没做到」。
- 原因：frontend/dist 被 .gitignore 排除，某次前端改动后没有重新 `npm run build`，
  静态托管一直服务旧产物。
- 解决：前端改动后必须重新 build，并用 node 校验 bundle 关键字符串
  （如 `api/data/search`）再部署；PLAYBOOK 新增检查项。

### [2026-08-04] 东财 push2 全系 502（代理节点问题）
- 现象：clist/stock/get 等 push2 接口持续 502，个股行情与板块同时挂。
- 解决：fetch_stock_quote 增加**腾讯 qt.gtimg.cn 备用**（实测可用）；
  板块已有 腾讯行业/浏览器 兜底；指数已有 东财 push2his/腾讯日K 兜底。
