import React from "react";
import MarkdownView from "./MarkdownView";
import { IndexMinuteChart, LimitLadderChart, SectorChart, SentimentChart, SentimentKpis } from "./Charts";
import { fmtDate, fmtDateTime, fmtTimeHM } from "../format";

const AVATAR_CLASSES = ["", "v2", "v3", "v4", "v5"];

export default function ReportView({ report, offline }) {
  const meta = report.meta || {};
  const snapshot = report.snapshot || {};
  const analyses = Array.isArray(report.analyses) ? report.analyses : [];
  const debate = Array.isArray(report.debate_history) ? report.debate_history : [];
  const degraded = Array.isArray(report.degraded) ? report.degraded : [];
  const responseCount = debate.reduce((s, r) => s + (Array.isArray(r.responses) ? r.responses.length : 0), 0);

  return (
    <div className="grid" style={{ gap: 16 }}>
      <div className="card result-head-card">
        <div className="result-head">
          <div>
            <div className="result-head-title">{meta.mode_label || "复盘"}报告{meta.date ? ` · ${fmtDate(meta.date)}` : ""}</div>
            <div className="result-head-sub">
              <span>日期：{fmtDate(meta.date)}</span>
              <span>时间点：{fmtTimeHM(meta.time)}</span>
              <span>创建：{fmtDateTime(meta.created_at)}</span>
              {offline ? <span className="pill offline">离线 mock</span> : null}
            </div>
            {meta.summary ? <div className="result-summary">{meta.summary}</div> : null}
          </div>
        </div>
        {degraded.length ? (
          <div className="degraded-banner">
            ⚠️ 本次复盘存在降级标注（数据或能力受限，结果可能不完整）：
            <ul className="degraded-list">
              {degraded.map((d) => (
                <li key={d}>{d}</li>
              ))}
            </ul>
          </div>
        ) : null}
        <div className="disclaimer" style={{ marginTop: 12 }}>
          <span>⚠️</span>
          <div>
            <b>免责声明：</b>
            {report.disclaimer || "仅供参考，不构成投资建议。市场有风险，入市需谨慎。"}
          </div>
        </div>
      </div>

      <div className="card">
        <h3 className="card-title">复盘报告（Markdown）</h3>
        <p className="card-sub">由趋势派 / 情绪派分析师点评、主持人总结与辩论后自动生成</p>
        <MarkdownView text={report.report} />
      </div>

      <div className="card">
        <h3 className="card-title">{analyses.length ? `${analyses.length} 位分析师点评` : "分析师点评"}</h3>
        <p className="card-sub">点击卡片展开查看完整点评与建议</p>
        {analyses.length ? (
          <div className="analyst-list">
            {analyses.map((an, i) => (
              <details key={an.skill_id || an.skill_name || i} className="analyst">
                <summary>
                  <span className={`avatar ${AVATAR_CLASSES[i % AVATAR_CLASSES.length]}`}>
                    {String(an.skill_name || "分").slice(0, 1)}
                  </span>
                  <span>
                    <span className="a-name">{an.skill_name || `分析师${i + 1}`}</span>
                  </span>
                  {Array.isArray(an.tags) && an.tags.length ? (
                    <span className="a-tags">
                      {an.tags.map((t) => (
                        <span key={t} className="tag-chip">{t}</span>
                      ))}
                    </span>
                  ) : null}
                </summary>
                <div className="analyst-content">
                  <MarkdownView text={an.analysis || "（暂无点评内容）"} />
                  {an.suggestion ? (
                    <div className="a-suggestion">
                      <div className="a-suggestion-title">💡 操作建议</div>
                      <MarkdownView text={an.suggestion} />
                    </div>
                  ) : null}
                </div>
              </details>
            ))}
          </div>
        ) : (
          <div className="empty">后端未返回分析师点评记录</div>
        )}
      </div>

      <div className="debate-block">
        <details>
          <summary>
            🗣️ 辩论记录（{debate.length} 轮 / {responseCount} 条）
          </summary>
          <div>
            {debate.length ? (
              debate.map((r, ri) => (
                <div key={ri} className="debate-round">
                  <div className="debate-round-head">
                    第 {r.round || ri + 1} 轮{r.topic ? ` · ${r.topic}` : ""}
                  </div>
                  {(Array.isArray(r.responses) ? r.responses : []).map((resp, i) => (
                    <div key={i} className="debate-item">
                      <span className="debate-speaker">{resp.skill_name || `分析师 ${i + 1}`}</span>
                      <div style={{ marginTop: 4 }}>{resp.response || ""}</div>
                    </div>
                  ))}
                </div>
              ))
            ) : (
              <div className="empty">本期无辩论记录</div>
            )}
          </div>
        </details>
      </div>

      <div className="card">
        <h3 className="card-title">数据快照</h3>
        <p className="card-sub">
          当日取数快照（比率已 ×100 展示；来源：{snapshot.source || "—"}）
          {snapshot.degraded && snapshot.degraded.length
            ? ` · 降级：${snapshot.degraded.join("；")}`
            : ""}
        </p>
        <div className="chart-grid">
          {snapshot.index_minute &&
          snapshot.index_minute.some(
            (ix) => ix && Array.isArray(ix.points) && ix.points.length > 0
          ) ? (
            <div className="chart-block">
              <div className="chart-title">指数分时</div>
              <IndexMinuteChart indices={snapshot.index_minute} />
            </div>
          ) : null}
          <div className="chart-block">
            <div className="chart-title">涨跌停梯队</div>
            <LimitLadderChart ladder={snapshot.limit_ladder} />
          </div>
          <div className="chart-block">
            <div className="chart-title">板块涨幅榜</div>
            <SectorChart sectors={snapshot.sectors} />
          </div>
          <div className="chart-block">
            <div className="chart-title">情绪指标</div>
            <SentimentChart sentiment={snapshot.sentiment} />
          </div>
        </div>
        <div style={{ marginTop: 14 }}>
          <SentimentKpis sentiment={snapshot.sentiment} />
        </div>
        {snapshot.sentiment && snapshot.sentiment.degraded && snapshot.sentiment.degraded.length ? (
          <div className="snapshot-note">
            情绪数据降级：{snapshot.sentiment.degraded.join("；")}
          </div>
        ) : null}
      </div>
    </div>
  );
}
