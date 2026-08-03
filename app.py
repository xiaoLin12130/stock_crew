"""Stock Review Crew — Streamlit 前端"""

import sys
from datetime import date, timedelta
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).parent / "src"))

from stock_review_crew.main import run
from stock_review_crew.skills import list_skills

st.set_page_config(page_title="A股多智能体复盘", page_icon="📈", layout="wide")
st.title("📈 A股多智能体复盘系统")
st.caption(f"参与分析师: {', '.join(s['name'] for s in list_skills())} | LangGraph + DeepSeek")

c1, c2 = st.columns([2, 1])
with c1:
    d = st.date_input("复盘日期", value=date.today() - timedelta(days=1), max_value=date.today())
with c2:
    r = st.slider("最大讨论轮次", 1, 5, 2)

if st.button("🚀 开始复盘", type="primary", use_container_width=True):
    ds = d.strftime("%Y-%m-%d")
    with st.spinner(f"⏳ 正在复盘 {ds}，预计 2-3 分钟..."):
        import os
        os.environ["STREAMLIT_RUNNING"] = "1"
        try:
            result = run(ds, max_rounds=r, verbose=False)
        except Exception as e:
            st.error(f"复盘失败: {e}")
            st.stop()

    # 显示结果
    report = result.get("final_report", "")
    st.success(f"✅ 复盘完成 | 讨论 {result.get('round_count',0)} 轮 | {len(result.get('analyses',[]))} 位分析师")

    t1, t2, t3 = st.tabs(["📋 完整报告", "🧠 分析师观点", "💬 讨论记录"])

    with t1:
        if report:
            for sec in report.split("\n## "):
                st.markdown(sec if sec.startswith("#") else f"## {sec}")

    with t2:
        idx = report.find("## 二、各分析师核心观点")
        if idx >= 0:
            end = report.find("\n## ", idx + 10)
            section = report[idx:end] if end > 0 else report[idx:]
            section = section.split("\n", 1)[1] if "\n" in section else section
            st.markdown(section.strip())
        else:
            for a in result.get("analyses", []):
                with st.expander(a.get("skill_name", "?")):
                    st.markdown(a.get("analysis", ""))

    with t3:
        for d in result.get("debate_history", []):
            st.subheader(f"第{d.get('round','?')}轮: {d.get('topic','暂无议题')}")
            for r in d.get("responses", []):
                with st.expander(r.get("skill_name", "?")):
                    st.markdown(r.get("response", ""))

st.divider()
st.caption("数据源: 通达信 + 同花顺 + Tushare")
