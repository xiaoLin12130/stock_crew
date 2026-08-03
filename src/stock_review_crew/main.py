"""入口：运行股票复盘流程（流式输出 + 写入 .md 日志）"""

import json
from datetime import datetime
from pathlib import Path

from .graph import build_graph
from .knowledge_store import auto_save
from .skills import list_skills

# 输出目录
OUTPUT_DIR = Path(__file__).parent.parent.parent / "output"

# 全局日志文件句柄
_log_file = None


def run(date: str, max_rounds: int = 3, verbose: bool = True):
    """
    运行复盘流程。

    参数:
        date: 日期字符串，如 "2026-07-16"
        max_rounds: 最大讨论轮次，默认 3
        verbose: 是否流式打印中间过程

    返回:
        {
            "final_report": str,
            "analyses": list[dict],
            "debate_history": list[dict],
            "round_count": int,
            "market_data": str,
        }
    """
    global _log_file

    # ── 创建日志文件 ──
    day_dir = OUTPUT_DIR / date
    day_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%H%M%S")
    log_path = day_dir / f"复盘_{timestamp}.md"
    _log_file = open(log_path, "w", encoding="utf-8")

    _write_log(f"# {date} A股复盘日志\n")
    _write_log(f"**开始时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
    _write_log(f"**最大讨论轮次**: {max_rounds}\n")
    skill_names = [s["name"] for s in list_skills()]
    _write_log(f"**参与分析师**: {', '.join(skill_names)}\n\n")
    _write_log("---\n")

    # ── 读取昨日复盘报告 ──
    yesterday_report = ""
    try:
        # 找上一个交易日目录（跳过当天及以后）
        date_dirs = sorted(
            [d for d in OUTPUT_DIR.iterdir() if d.is_dir() and d.name < date],
            reverse=True
        )
        for d in date_dirs:
            reports = sorted(d.glob("复盘_*.md"), reverse=True)
            if reports:
                with open(reports[0], "r", encoding="utf-8") as f:
                    full = f.read()
                # 提取核心段落：找"各分析师核心观点"之后的内容
                idx = full.find("## 二、各分析师核心观点")
                if idx < 0:
                    idx = full.find("## 四、明日操作计划")
                if idx >= 0:
                    # 取从该位置到"## 五"或文末之间，不超过6000字
                    end = full.find("\n## 五", idx)
                    yesterday_report = full[idx:end] if end > 0 else full[idx:idx+6000]
                else:
                    yesterday_report = full[:6000]
                if verbose:
                    print(f"📂 读取昨日报告: {reports[0].parent.name}/{reports[0].name} (提取{len(yesterday_report)}字)")
                break
    except Exception:
        pass

    app = build_graph()

    initial_state = {
        "date": date,
        "max_rounds": max_rounds,
        "market_data": "",
        "analyses": [],
        "discussion_topic": "",
        "discussion_done": False,
        "round_count": 0,
        "skill_names": [],
        "debate_history": [],
        "final_report": "",
        "yesterday_report": yesterday_report,
        "raw_news": "",
    }

    # ── 执行 ──
    import os as _os
    if not _os.environ.get("STREAMLIT_RUNNING"):
        print("⏳ 正在拉取行情数据...")
    final_state = app.invoke(initial_state)
    if not _os.environ.get("STREAMLIT_RUNNING"):
        print(f"✅ 分析完成: {len(final_state.get('analyses',[]))}位分析师, {len(final_state.get('final_report',''))}字报告")

    # ── 写入行情数据到 .md ──
    _print_node_output("fetch_data", final_state)

    # ── 写入 .md 日志 ──
    for a in final_state.get("analyses", []):
        _write_log(f"\n### {a['skill_name']}\n\n{a.get('analysis', '')}\n\n")
    _write_log(f"\n📝 报告完成 ({len(final_state.get('final_report',''))}字)\n")
    _write_log("\n---\n")
    _write_log(f"\n**结束时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")

    result = {
        "final_report": final_state.get("final_report", ""),
        "analyses": final_state.get("analyses", []),
        "debate_history": final_state.get("debate_history", []),
        "round_count": final_state.get("round_count", 0),
        "market_data": final_state.get("market_data", ""),
    }

    _write_log(f"\n# 最终报告\n\n{result['final_report']}\n")

    # ── 自动存档到知识库 ──
    try:
        auto_save(date, result.get("analyses", []), result.get("final_report", ""))
    except Exception:
        pass

    _log_file.close()
    _log_file = None

    import os
    if not os.environ.get("STREAMLIT_RUNNING"):
        print(f"\n📄 日志已保存: {log_path}")

    return result


def _write_log(text: str):
    """同时写入日志文件和打印到控制台。"""
    import os
    if not os.environ.get("STREAMLIT_RUNNING"):
        print(text, end="")
    if _log_file:
        _log_file.write(text)
        _log_file.flush()


def _ts() -> str:
    """当前时间戳，格式: [HH:MM:SS]"""
    return f"[{datetime.now().strftime('%H:%M:%S')}]"


def _print_node_output(node_name: str, update: dict):
    """按节点类型格式化输出（同时写入日志文件和控制台）。"""
    divider = "─" * 60

    if node_name == "fetch_data":
        _write_log(f"\n{divider}\n")
        _write_log(f"{_ts()} 📊 **Node1 — 行情数据获取完毕**\n\n")

        raw = update.get("market_data", "")
        if not raw:
            _write_log("无数据\n")
        else:
            # ── 提取三个 JSON 块 ──
            markers = [
                ("## 一、今日市场微观数据", "micro"),
                ("## 二、近60日指数趋势", "trend"),
                ("## 三、宏观外围数据", "macro"),
                ("## 四、昨日涨停今日表现", "sentiment"),
                ("## 五、今日财经资讯", "news"),
            ]
            blocks = {}
            for i, (marker, key) in enumerate(markers):
                start = raw.find(marker)
                if start < 0:
                    continue
                json_start = raw.find("{", start)
                next_start = raw.find("## ", json_start + 1) if json_start > 0 else -1
                json_str = raw[json_start:next_start] if next_start > 0 else raw[json_start:]
                try:
                    blocks[key] = json.loads(json_str.strip())
                except Exception:
                    blocks[key] = None

            # ── 打印微观数据 ──
            micro = blocks.get("micro", {})
            if micro:
                idx = micro.get("index", {})
                _write_log("### 指数表现\n\n")
                for key, label in [("shanghai", "上证"), ("shenzhen", "深证"), ("chuangye", "创业板"), ("kechuang", "科创50")]:
                    v = idx.get(key, {})
                    if not v or "error" in v:
                        continue
                    vol = v.get('volume')
                    vol_vs = v.get('volume_vs_prev_pct')
                    sign = '+' if (vol_vs or 0) > 0 else ''
                    vol_yi = vol / 1_0000_0000 if vol else 0
                    _write_log(f"- {label}: 收**{v.get('close','-')}** "
                               f"开{v.get('open','-')} 高{v.get('high','-')} 低{v.get('low','-')} "
                               f"| {v.get('pct_change','-')}% | 成交量{vol_yi:.1f}亿手({sign}{vol_vs}%)\n")
                # 全市场总量
                breadth = micro.get("market_breadth", {})
                total = breadth.get("total_volume")
                if total:
                    _write_log(f"\n> 全市场量合计: **{total:.0f}**\n")

                zt = micro.get("zhangting", {})
                _write_log(f"\n### 涨停板\n\n")
                _write_log(f"- 涨停总数: **{zt.get('total', 'N/A')}** 只\n")
                tier = zt.get("tier", {})
                if tier:
                    _write_log(f"- 连板梯队: {' | '.join(f'{k}{"板" if k != "首板" else ""}:{v}只' for k,v in tier.items())}\n")
                top_ind = zt.get("top_industries", {})
                if top_ind:
                    _write_log(f"- 涨停集中行业: {' / '.join(f'{k}({v}只)' for k,v in list(top_ind.items())[:5])}\n")

                dt = micro.get("dieting", {})
                _write_log(f"\n### 跌停板\n\n")
                _write_log(f"- 跌停总数: **{dt.get('total', 'N/A')}** 只\n")

                sectors = micro.get("sectors", {})
                if sectors.get("top5"):
                    _write_log(f"\n### 板块涨幅前五 ({sectors.get('source','')})\n\n")
                    has_leader = "领涨股" in sectors["top5"][0]
                    if has_leader:
                        _write_log("| 排名 | 板块 | 涨跌幅 | 领涨股 | 领涨涨幅 | 净流入(亿) | 涨/跌家数 |\n")
                        _write_log("|------|------|--------|--------|----------|-----------|----------|\n")
                        for i, s in enumerate(sectors["top5"], 1):
                            _write_log(
                                f"| {i} | {s.get('板块','')} | {s.get('涨跌幅','')}% | "
                                f"{s.get('领涨股','')} | {s.get('领涨股-涨跌幅','')}% | "
                                f"{s.get('净流入','')} | {s.get('上涨家数','')}/{s.get('下跌家数','')} |\n"
                            )
                    else:
                        _write_log("| 排名 | 板块 | 涨跌幅 | 开盘 | 最高 | 最低 | 收盘 | 成交额(亿) | 量变化 |\n")
                        _write_log("|------|------|--------|------|------|------|------|------------|--------|\n")
                        for i, s in enumerate(sectors["top5"], 1):
                            amt = s.get('成交额', 0)
                            amt_chg = s.get('成交额变化', 0)
                            sign = '+' if amt_chg > 0 else ''
                            _write_log(
                                f"| {i} | {s.get('板块','')} | {s.get('涨跌幅','')}% | "
                                f"{s.get('开盘','-')} | {s.get('最高','-')} | {s.get('最低','-')} | "
                                f"{s.get('收盘','-')} | {amt/1e8:.1f} | {sign}{amt_chg}% |\n"
                            )

                    # ── 净流出前五（仅实时数据有）──
                    if sectors.get("bottom5_flow"):
                        _write_log(f"\n### 板块净流出前五\n\n")
                        _write_log("| 排名 | 板块 | 涨跌幅 | 领涨股 | 净流入(亿) |\n")
                        _write_log("|------|------|--------|--------|-----------|\n")
                        for i, s in enumerate(sectors["bottom5_flow"], 1):
                            _write_log(
                                f"| {i} | {s.get('板块','')} | {s.get('涨跌幅','')}% | "
                                f"{s.get('领涨股','')} | {s.get('净流入','')} |\n"
                            )

            # ── 打印情绪数据 ──
            sentiment = blocks.get("sentiment", {})
            if sentiment and "error" not in sentiment:
                _write_log(f"\n### 昨日涨停今日表现\n\n")
                red = sentiment.get("red_rate_pct", "?")
                lian = sentiment.get("lianban_rate_pct", "?")
                hean = sentiment.get("hean_rate_pct", "?")
                avg = sentiment.get("avg_return_pct", "?")
                _write_log(f"- 昨日{int(sentiment.get('yesterday_zt_count',0))}只涨停，今日平均收益 **{avg}%**\n")
                _write_log(f"- 红盘率 **{red}%** | 连板率 **{lian}%**({int(sentiment.get('lianban_count',0))}只) | 核按钮率 **{hean}%**({int(sentiment.get('hean_count',0))}只)\n")

                mb = sentiment.get("market_breadth", {})
                if mb:
                    dist = mb.get("distribution", {})
                    _write_log(f"\n### 全市场涨跌分布 ({mb.get('total',0)}只)\n\n")
                    _write_log(f"- 上涨 **{mb.get('up',0)}** 只 | 下跌 **{mb.get('down',0)}** 只 | 涨跌比 {mb.get('up_down_ratio','?')}\n")
                    _write_log(f"- 摸过涨停 **{mb.get('touched_zt',0)}** 只 | 炸板 **{mb.get('zhaban',0)}** 只 | 炸板率 **{mb.get('zhaban_rate_pct',0)}%**\n")
                    _write_log(f"- 涨停 {dist.get('涨停',0)} | 涨7-10% {dist.get('涨7-10%',0)} | 涨5-7% {dist.get('涨5-7%',0)} | 涨2-5% {dist.get('涨2-5%',0)} | 涨0-2% {dist.get('涨0-2%',0)}\n")
                    _write_log(f"- 跌停 {dist.get('跌停',0)} | 跌7-10% {dist.get('跌7-10%',0)} | 跌5-7% {dist.get('跌5-7%',0)} | 跌2-5% {dist.get('跌2-5%',0)} | 跌0-2% {dist.get('跌0-2%',0)}\n")

            # ── 打印资讯 ──
            news = blocks.get("news", {})
            if news:
                cctv = news.get("cctv", [])
                if cctv:
                    _write_log(f"\n### 今日要闻\n\n")
                    for t in cctv[:5]:
                        _write_log(f"- {t}\n")
                cx = news.get("caixin", [])
                if cx:
                    _write_log(f"\n### 财新头条\n\n")
                    for c in cx[:5]:
                        _write_log(f"- [{c.get('tag','')}] {c.get('summary','')[:120]}\n")

            # ── 打印趋势数据 ──
            trend = blocks.get("trend", {})
            if trend:
                _write_log("\n### 近60日均线系统\n\n")
                _write_log("| 指数 | 收盘 | MA5 | MA10 | MA13 | MA20 | MA34 | MA60 | MA144 | MA250 | 5日 | 20日 | 量能 |\n")
                _write_log("|------|------|-----|------|------|------|------|------|-------|-------|-----|------|------|\n")
                for key, label in [("shanghai", "上证"), ("shenzhen", "深证"), ("chuangye", "创业板"), ("kechuang", "科创50")]:
                    t = trend.get(key, {})
                    if not t or "error" in t:
                        continue
                    _write_log(
                        f"| {label} | {t.get('current_close')} | "
                        f"{t.get('ma5', '-')} | {t.get('ma10', '-')} | "
                        f"{t.get('ma13', '-')} | {t.get('ma20', '-')} | "
                        f"{t.get('ma34', '-')} | {t.get('ma60', '-')} | "
                        f"{t.get('ma144', '-')} | {t.get('ma250', '-')} | "
                        f"{t.get('pct_5d', '-')}% | {t.get('pct_20d', '-')}% | "
                        f"{t.get('volume_trend', '-')} |\n"
                    )

            # ── 打印宏观数据 ──
            macro = blocks.get("macro", {})
            if macro:
                ext = macro.get("external", {})
                nasdaq = ext.get("nasdaq")
                if isinstance(nasdaq, dict):
                    _write_log(f"\n### 外围市场\n\n")
                    _write_log(f"- 纳斯达克: {nasdaq.get('close','N/A')} ({nasdaq.get('pct_change','N/A')}%)\n")
                elif nasdaq is None:
                    pass  # 历史复盘无纳斯达克数据

                fut = macro.get("futures", {})
                if fut:
                    _write_log(f"\n### 期货\n\n")
                    for fk, fl in [("IH", "上证50"), ("IM", "中证1000")]:
                        fv = fut.get(fk, {})
                        if isinstance(fv, dict):
                            _write_log(f"- {fl}({fk}): 收{fv.get('close','N/A')} 量{fv.get('volume','N/A')} 持仓{fv.get('open_interest','N/A')}\n")

        _write_log(f"\n{divider}\n")

    elif node_name == "news_analyst":
        _write_log(f"\n📰 资讯分析完成\n")

    elif node_name in ("analysts", "trend_analysts"):
        _write_log(f"\n{divider}\n")
        _write_log(f"{_ts()} 📊 **Node2a — 趋势分析师（阿狼、冰川）**\n")
        analyses = update.get("analyses", [])
        for a in analyses:
            if a.get("group") not in ("trend", "both"):
                continue
            tool_tag = ""
            if a.get("tool_used"):
                codes = [t["code"] for t in a.get("tool_calls", [])]
                tool_tag = f" 🔧查:{','.join(codes)}"
            _write_log(f"\n### {a['skill_name']}{tool_tag}\n\n")
            _write_log(f"{a.get('analysis', '')}\n\n")
        _write_log(f"{divider}\n")

    elif node_name == "sentiment_analysts":
        _write_log(f"\n{divider}\n")
        _write_log(f"{_ts()} 🎯 **Node2b — 情绪分析师（拔小弦、养家）**\n")
        analyses = update.get("analyses", [])
        for a in analyses:
            if a.get("group") != "sentiment":
                continue
            tool_tag = ""
            if a.get("tool_used"):
                codes = [t["code"] for t in a.get("tool_calls", [])]
                tool_tag = f" 🔧查:{','.join(codes)}"
            _write_log(f"\n### {a['skill_name']}{tool_tag}\n\n")
            _write_log(f"{a.get('analysis', '')}\n\n")
        _write_log(f"{divider}\n")

    elif node_name == "host":
        _write_log(f"\n{divider}\n")
        _write_log(f"{_ts()} 📋 **Node3 — 主持人判断**\n\n")
        done = update.get("discussion_done", True)
        topic = update.get("discussion_topic", "")
        rnd = update.get("round_count", "?")
        status = "✅ 讨论完毕" if done else "🔄 需要继续讨论"
        _write_log(f"- 第 **{rnd}** 轮\n")
        _write_log(f"- 状态: **{status}**\n")
        if topic:
            _write_log(f"- 分歧议题: **{topic}**\n")
        else:
            _write_log(f"- 无实质性分歧\n")
        _write_log(f"\n{divider}\n")

    elif node_name == "debate":
        _write_log(f"\n{divider}\n")
        _write_log(f"{_ts()} 💬 **Node4 — 辩论讨论**\n")
        history = update.get("debate_history", [])
        for entry in history:
            _write_log(f"\n## 第 {entry['round']} 轮辩论：{entry['topic']}\n\n")
            for r in entry.get("responses", []):
                _write_log(f"### {r['skill_name']}\n\n")
                _write_log(f"{r.get('response', '')}\n\n")
        _write_log(f"{divider}\n")

    elif node_name == "report":
        _write_log(f"\n{divider}\n")
        _write_log(f"{_ts()} 📝 **Node5 — 最终报告生成完毕**\n")
        report = update.get("final_report", "")
        _write_log(f"- 报告长度: **{len(report)}** 字\n")
        _write_log(f"\n{divider}\n")


if __name__ == "__main__":
    import sys

    date = sys.argv[1] if len(sys.argv) > 1 else "2026-07-15"
    rounds = int(sys.argv[2]) if len(sys.argv) > 2 else 3

    print(f"🚀 开始复盘 {date}，最大讨论轮次: {rounds}")
    skill_names = [s["name"] for s in list_skills()]
    print(f"   参与分析师: {len(skill_names)} 位（{', '.join(skill_names)}）")

    result = run(date, rounds)

    print(f"\n{'═' * 60}")
    print(f"🏁 复盘完成")
    print(f"   讨论轮次: {result['round_count']}")
    print(f"   分析师数量: {len(result['analyses'])}")
    print(f"   讨论记录数: {len(result['debate_history'])}")
    print(f"{'═' * 60}\n")

    print(result["final_report"])
