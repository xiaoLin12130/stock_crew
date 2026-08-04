import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import * as api from "../api";
import { ANALYSTS } from "../mock";
import { currentMode, DEFAULT_MODE, MODES, modeLabel, nowMinutes, todayStr, windowHints, isTradingDay } from "../modes";
import { fmtDate, fmtTimeHM } from "../format";

export default function HomeView({
  onStartReview,
  onOpenReview,
  onDeleteReview,
  history,
  onRetryHistory,
  onOpenChat,
}) {
  const [date, setDate] = useState(todayStr());
  const [mode, setMode] = useState(DEFAULT_MODE);
  const [maxRounds, setMaxRounds] = useState(2);
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState("");
  const [autoModeNote, setAutoModeNote] = useState("");

  const [targetType, setTargetType] = useState("stock");
  const [target, setTarget] = useState("");
  const [selectedAnalysts, setSelectedAnalysts] = useState([]);
  const [chatError, setChatError] = useState("");

  // ── 实时数据板块 ──
  const [rtData, setRtData] = useState(null);
  const [rtLoading, setRtLoading] = useState(false);
  const [rtError, setRtError] = useState("");
  const [rtAuto, setRtAuto] = useState(false);
  const rtTimer = useRef(null);
  const [quoteCode, setQuoteCode] = useState("");
  const [quote, setQuote] = useState(null);
  const [quoteLoading, setQuoteLoading] = useState(false);
  const [quoteError, setQuoteError] = useState("");
  const [quoteMatches, setQuoteMatches] = useState([]);

  const loadRealtime = useCallback(async () => {
    setRtLoading(true);
    setRtError("");
    try {
      const d = await api.getRealtimeData();
      setRtData(d);
    } catch (err) {
      setRtError((err && err.message) || "实时数据加载失败");
    } finally {
      setRtLoading(false);
    }
  }, []);

  useEffect(() => {
    loadRealtime();
    return () => {
      if (rtTimer.current) clearInterval(rtTimer.current);
    };
  }, [loadRealtime]);

  useEffect(() => {
    if (rtTimer.current) clearInterval(rtTimer.current);
    if (rtAuto) {
      rtTimer.current = setInterval(loadRealtime, 30000);
    }
    return () => {
      if (rtTimer.current) clearInterval(rtTimer.current);
    };
  }, [rtAuto, loadRealtime]);

  // 时间模式自动选择：日期为今天时按当前时间点选择最合适模式
  const applyAutoMode = useCallback((dateStr) => {
    if (dateStr !== todayStr()) {
      setAutoModeNote("");
      return;
    }
    if (!isTradingDay(dateStr)) {
      setAutoModeNote("今天非交易日，未自动选择盘中模式；可手动选择历史补做模式。");
      return;
    }
    const cur = currentMode(nowMinutes());
    setMode(cur);
    setAutoModeNote(`已按当前时间自动选择：${modeLabel(cur)}（可手动切换）`);
  }, []);

  useEffect(() => {
    applyAutoMode(date);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const loadQuote = useCallback(async (code) => {
    const c = String(code || "").trim();
    if (!/^\d{6}$/.test(c)) {
      setQuoteError("请输入 6 位股票代码，或先搜索股票名称选择代码");
      return;
    }
    setQuoteLoading(true);
    setQuoteError("");
    try {
      const q = await api.getStockQuote(c);
      setQuote(q);
    } catch (err) {
      setQuoteError((err && err.message) || "行情查询失败");
      setQuote(null);
    } finally {
      setQuoteLoading(false);
    }
  }, []);

  // 支持名称查询：6 位代码直接查，否则先搜索再展示候选
  const handleQuoteQuery = useCallback(
    async (text) => {
      const t = String(text || "").trim();
      if (!t) {
        setQuoteError("请输入股票名称或 6 位代码");
        return;
      }
      setQuoteMatches([]);
      if (/^\d{6}$/.test(t)) {
        loadQuote(t);
        return;
      }
      setQuoteLoading(true);
      setQuoteError("");
      try {
        const list = await api.searchStocks(t);
        if (!list || list.length === 0) {
          setQuoteError(`未找到与「${t}」匹配的股票`);
          return;
        }
        if (list.length === 1) {
          loadQuote(list[0].code);
          return;
        }
        setQuoteMatches(list.slice(0, 5));
      } catch (err) {
        setQuoteError((err && err.message) || "搜索失败，请重试");
      } finally {
        setQuoteLoading(false);
      }
    },
    [loadQuote]
  );

  const hints = useMemo(() => windowHints(mode, date), [mode, date]);

  const handleSubmit = async () => {
    setSubmitError("");
    if (!date) {
      setSubmitError("请选择复盘日期");
      return;
    }
    setSubmitting(true);
    try {
      await onStartReview({ date, mode, max_rounds: maxRounds });
    } catch (err) {
      setSubmitError((err && err.message) || "提交复盘任务失败，请稍后重试");
    } finally {
      setSubmitting(false);
    }
  };

  const toggleAnalyst = (id) => {
    setSelectedAnalysts((prev) =>
      prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id]
    );
    setChatError("");
  };

  const handleOpenChat = () => {
    setChatError("");
    const t = target.trim();
    if (!t) {
      setChatError(targetType === "stock" ? "请输入股票代码或名称" : "请输入板块名称");
      return;
    }
    if (selectedAnalysts.length === 0) {
      setChatError("请至少选择一位分析师");
      return;
    }
    onOpenChat({
      target_type: targetType,
      target: t,
      target_name: t,
      analysts: selectedAnalysts,
      title: `${t} · 分析师对话`,
    });
  };

  const groups = history.groups || [];

  return (
    <div className="home-grid">
      {/* 复盘表单 */}
      <div className="card">
        <h3 className="card-title">新建复盘</h3>
        <p className="card-sub">选择日期与时间模式，系统将按 6 模式感知流水线自动生成复盘报告</p>

        <div className="form-row">
          <label className="form-label" htmlFor="review-date">复盘日期</label>
          <input
            id="review-date"
            type="date"
            className="form-input"
            value={date}
            onChange={(e) => {
              setDate(e.target.value);
              applyAutoMode(e.target.value);
            }}
          />
          <div className="form-note">历史日期可补做任意模式；数据源无历史时将标注「数据缺失」并继续流程</div>
        </div>

        <div className="form-row">
          <label className="form-label">时间模式</label>
          <div className="form-note">
            {autoModeNote || "系统将在打开页面时按当前时间自动选择；历史日期请手动选择"}
            {autoModeNote ? (
              <button
                type="button"
                className="btn btn-ghost btn-xs"
                style={{ marginLeft: 8 }}
                onClick={() => applyAutoMode(date)}
              >
                重新按当前时间选择
              </button>
            ) : null}
          </div>
          <div className="mode-grid">
            {MODES.map((m) => (
              <button
                key={m.code}
                type="button"
                className={`mode-card${mode === m.code ? " active" : ""}`}
                onClick={() => setMode(m.code)}
                title={m.desc}
              >
                <div className="mode-name">{m.label}</div>
                <div className="mode-window">{m.window}</div>
              </button>
            ))}
          </div>
          <div className="mode-desc">
            <b>{modeLabel(mode)}</b>（{MODES.find((m) => m.code === mode).window}）：{MODES.find((m) => m.code === mode).desc}
          </div>
        </div>

        {hints.length ? (
          <div className="hint-box">
            {hints.map((h) => (
              <div key={h}>💡 {h}</div>
            ))}
          </div>
        ) : null}

        <div className="form-row">
          <label className="form-label" htmlFor="review-rounds">辩论轮数（可选）</label>
          <select
            id="review-rounds"
            className="form-input form-select"
            value={maxRounds}
            onChange={(e) => setMaxRounds(Number(e.target.value))}
          >
            <option value={1}>1 轮（快速）</option>
            <option value={2}>2 轮（默认）</option>
            <option value={3}>3 轮（深度，≤3 轮上限）</option>
          </select>
        </div>

        {submitError ? <div className="upload-error">{submitError}</div> : null}
        <div className="form-actions">
          <button className="btn btn-accent" disabled={submitting} onClick={handleSubmit}>
            {submitting ? "正在提交…" : "开始复盘"}
          </button>
        </div>
      </div>

      <div className="home-right">
        {/* 聊天卡片 */}
        <div className="card">
          <h3 className="card-title">分析师对话</h3>
          <p className="card-sub">选择标的与分析团队，多轮交叉问答（单分析师直接回复，多分析师逐位表态后汇总）</p>

          <div className="seg">
            <button
              className={`seg-btn${targetType === "stock" ? " active" : ""}`}
              onClick={() => setTargetType("stock")}
            >
              个股
            </button>
            <button
              className={`seg-btn${targetType === "sector" ? " active" : ""}`}
              onClick={() => setTargetType("sector")}
            >
              板块
            </button>
          </div>
          <input
            className="form-input"
            style={{ marginTop: 10 }}
            placeholder={targetType === "stock" ? "输入股票代码或名称，如 600519 / 贵州茅台" : "输入板块名称，如 半导体 / 券商"}
            value={target}
            onChange={(e) => setTarget(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") handleOpenChat();
            }}
          />

          <div className="analyst-pick">
            {ANALYSTS.map((a) => {
              const on = selectedAnalysts.includes(a.id);
              return (
                <button
                  key={a.id}
                  type="button"
                  className={`pick-chip${on ? " on" : ""}`}
                  onClick={() => toggleAnalyst(a.id)}
                >
                  {on ? "✓ " : ""}{a.skill_name}
                </button>
              );
            })}
          </div>

          {chatError ? <div className="upload-error">{chatError}</div> : null}
          <div className="form-actions">
            <button className="btn btn-accent" onClick={handleOpenChat}>进入聊天</button>
          </div>
        </div>

        {/* 历史分组预览 */}
        <div className="card">
          <div className="card-head-row">
            <h3 className="card-title">历史复盘</h3>
            {history.offline ? <span className="pill offline">离线 mock</span> : null}
          </div>
          {history.loading ? (
            <div className="loading-card" style={{ padding: 28 }}>
              <div className="spinner" />
              <div style={{ marginTop: 10 }}>正在加载历史复盘…</div>
            </div>
          ) : history.error ? (
            <div className="error-panel" style={{ padding: 24 }}>
              <div className="error-title">加载失败</div>
              <div className="error-detail">{history.error}</div>
              <button className="btn btn-outline btn-sm" onClick={onRetryHistory}>重试</button>
            </div>
          ) : groups.length === 0 ? (
            <div className="empty">
              暂无历史复盘
              <br />
              完成一次复盘后会自动保存，并按日期分组展示
            </div>
          ) : (
            <div className="home-history">
              {groups.map((g) => (
                <div key={g.date} className="hh-group">
                  <div className="hh-date">
                    {fmtDate(g.date)}
                    <span className="hh-count">{g.items.length} 份</span>
                  </div>
                  {g.items.map((it) => (
                    <div
                      key={`${g.date}/${it.time}`}
                      role="button"
                      tabIndex={0}
                      className="hh-item"
                      onClick={() => onOpenReview(g.date, it.time)}
                      onKeyDown={(e) => {
                        if (e.key === "Enter" || e.key === " ") {
                          e.preventDefault();
                          onOpenReview(g.date, it.time);
                        }
                      }}
                    >
                      <span className="hh-time">{fmtTimeHM(it.time)}</span>
                      <span className="tag-chip light">{it.mode_label}</span>
                      <span className="hh-summary" title={it.summary}>{it.summary || "（无摘要）"}</span>
                      <button
                        className="hh-del"
                        title="删除该复盘"
                        onClick={(e) => {
                          e.stopPropagation();
                          if (window.confirm(`确认删除「${fmtTimeHM(it.time)} ${it.mode_label}」这份复盘？删除后不可恢复。`)) {
                            onDeleteReview(g.date, it.time).catch(() => {});
                          }
                        }}
                      >
                        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                          <path d="M3 6h18" />
                          <path d="M8 6V4a1 1 0 0 1 1-1h6a1 1 0 0 1 1 1v2" />
                          <path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6" />
                        </svg>
                      </button>
                    </div>
                  ))}
                </div>
              ))}
            </div>
          )}
        </div>
      </div>

      {/* 实时数据板块 */}
      <div className="card realtime-card">
        <div className="realtime-head">
          <div>
            <h3 className="card-title">实时数据</h3>
            <p className="card-sub">
              {rtData && rtData.status
                ? `${rtData.status.phase} · 更新于 ${rtData.status.time}`
                : "盘中实时行情（东财/同花顺/新浪）"}
              {rtData && rtData.sources && rtData.sources.length
                ? ` · 来源：${rtData.sources.join(" / ")}`
                : ""}
            </p>
          </div>
          <div className="realtime-actions">
            <label className="rt-auto">
              <input type="checkbox" checked={rtAuto} onChange={(e) => setRtAuto(e.target.checked)} />
              30 秒自动刷新
            </label>
            <button className="btn btn-outline btn-sm" onClick={loadRealtime} disabled={rtLoading}>
              {rtLoading ? "刷新中…" : "刷新"}
            </button>
          </div>
        </div>

        {rtError ? <div className="upload-error">{rtError}</div> : null}
        {rtData && rtData.degraded && rtData.degraded.length ? (
          <div className="degraded-banner" style={{ marginBottom: 12 }}>
            部分数据降级：{rtData.degraded.join("；")}
          </div>
        ) : null}

        {!rtData && !rtError ? (
          <div className="loading-card">
            <div className="spinner" />
            <div style={{ marginTop: 10 }}>正在加载实时数据…</div>
          </div>
        ) : null}

        {rtData && rtData.indices ? (
          <div className="kpi-grid">
            {rtData.indices.indices.map((ix) => {
              const pct = Number(ix.pct_change);
              const cls = !Number.isFinite(pct) ? "zero" : pct > 0 ? "pos" : pct < 0 ? "neg" : "zero";
              return (
                <div key={ix.name} className="kpi">
                  <div className="kpi-label">{ix.name}</div>
                  <div className="kpi-value">
                    {Number.isFinite(Number(ix.price)) ? Number(ix.price).toFixed(2) : "—"}
                  </div>
                  <div className={`kpi-sub ${cls}`}>
                    {Number.isFinite(pct) ? `${pct >= 0 ? "+" : ""}${(pct * 100).toFixed(2)}%` : "—"}
                    {ix.source ? ` · ${ix.source}` : ""}
                  </div>
                </div>
              );
            })}
          </div>
        ) : null}

        {rtData && rtData.zt ? (
          <div className="rt-zt">
            <span className="tag-chip light">
              涨停 <b className="pos">{rtData.zt.limit_up_count ?? "—"}</b>
            </span>
            <span className="tag-chip light">
              跌停 <b className="neg">{rtData.zt.limit_down_count ?? "—"}</b>
            </span>
            <span className="tag-chip light">
              炸板率{" "}
              <b className="zero">
                {rtData.zt.zhaban_rate != null ? `${(rtData.zt.zhaban_rate * 100).toFixed(1)}%` : "—"}
              </b>
            </span>
            <span className="tag-chip light">
              封板率{" "}
              <b className="zero">
                {rtData.zt.seal_rate != null ? `${(rtData.zt.seal_rate * 100).toFixed(1)}%` : "—"}
              </b>
            </span>
            <span className="tag-chip light">
              昨日涨停 <b>{rtData.zt.yesterday_zt_count ?? "—"}</b>
            </span>
            {rtData.zt.tier && Object.keys(rtData.zt.tier).length ? (
              <span className="tag-chip light">
                梯队{" "}
                <b>
                  {Object.entries(rtData.zt.tier)
                    .map(([k, v]) => `${k} ${v}`)
                    .join(" / ")}
                </b>
              </span>
            ) : null}
          </div>
        ) : null}

        {rtData && rtData.sectors ? (
          <div className="rt-grid">
            <div className="rt-col">
              <div className="rt-col-title">板块涨幅 Top5</div>
              {rtData.sectors.top.map((s) => {
                const pct = Number(s.pct_change);
                const cls = pct > 0 ? "pos" : pct < 0 ? "neg" : "zero";
                return (
                  <div key={s.name} className="rt-row">
                    <span className="rt-name">{s.name}</span>
                    <span className={cls}>
                      {Number.isFinite(pct) ? `${pct >= 0 ? "+" : ""}${(pct * 100).toFixed(2)}%` : "—"}
                    </span>
                  </div>
                );
              })}
            </div>
            <div className="rt-col">
              <div className="rt-col-title">主力净流入 Top5（亿）</div>
              {(rtData.sectors.flow_in || []).map((s) => (
                <div key={s.name} className="rt-row">
                  <span className="rt-name">{s.name}</span>
                  <span className="pos">
                    {Number.isFinite(Number(s.net_inflow)) ? `${(Number(s.net_inflow) / 1e8).toFixed(1)} 亿` : "—"}
                  </span>
                </div>
              ))}
            </div>
            <div className="rt-col">
              <div className="rt-col-title">主力净流出 Top5（亿）</div>
              {(rtData.sectors.flow_out || []).map((s) => (
                <div key={s.name} className="rt-row">
                  <span className="rt-name">{s.name}</span>
                  <span className="neg">
                    {Number.isFinite(Number(s.net_inflow)) ? `${(Number(s.net_inflow) / 1e8).toFixed(1)} 亿` : "—"}
                  </span>
                </div>
              ))}
            </div>
          </div>
        ) : null}

        {rtData && rtData.news && rtData.news.news && rtData.news.news.length ? (
          <div className="rt-news">
            <div className="rt-col-title">最新快讯（新浪 7x24）</div>
            {rtData.news.news.slice(0, 6).map((n, i) => (
              <div key={i} className="rt-news-item">
                <span className="rt-news-time">{n.time || ""}</span>
                <span className="rt-news-text">{n.text}</span>
              </div>
            ))}
          </div>
        ) : null}

        {/* 个股实时行情 */}
        <div className="rt-quote">
          <div className="rt-col-title">个股实时行情</div>
          <div className="rt-quote-row">
            <input
              className="search-input"
              style={{ maxWidth: 200 }}
              placeholder="输入名称或代码，如：茅台 / 600519"
              value={quoteCode}
              onChange={(e) => setQuoteCode(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") handleQuoteQuery(quoteCode);
              }}
            />
            <button className="btn btn-outline btn-sm" onClick={() => handleQuoteQuery(quoteCode)} disabled={quoteLoading}>
              {quoteLoading ? "查询中…" : "查询"}
            </button>
          </div>
          {quoteError ? <div className="upload-error">{quoteError}</div> : null}
          {quoteMatches.length ? (
            <div className="rt-quote-matches">
              <span>找到多个匹配，请选择：</span>
              {quoteMatches.map((m) => (
                <button
                  key={m.code}
                  type="button"
                  className="tag-chip light"
                  onClick={() => {
                    setQuoteMatches([]);
                    loadQuote(m.code);
                  }}
                >
                  {m.name || m.code}（{m.code}）
                </button>
              ))}
            </div>
          ) : null}
          {quote ? (
            <div className="rt-quote-box">
              <div className="rt-quote-name">
                {quote.name || "—"}
                <span className="rt-quote-code">{quote.code}</span>
              </div>
              <div className={`rt-quote-price ${quote.pct_change > 0 ? "pos" : quote.pct_change < 0 ? "neg" : "zero"}`}>
                {Number.isFinite(Number(quote.price)) ? Number(quote.price).toFixed(2) : "—"}
              </div>
              <div className={`rt-quote-pct ${quote.pct_change > 0 ? "pos" : quote.pct_change < 0 ? "neg" : "zero"}`}>
                {Number.isFinite(Number(quote.pct_change))
                  ? `${quote.pct_change >= 0 ? "+" : ""}${(quote.pct_change * 100).toFixed(2)}%`
                  : "—"}
              </div>
              <div className="rt-quote-meta">
                <span>今开 {Number.isFinite(Number(quote.open)) ? Number(quote.open).toFixed(2) : "—"}</span>
                <span>最高 {Number.isFinite(Number(quote.high)) ? Number(quote.high).toFixed(2) : "—"}</span>
                <span>最低 {Number.isFinite(Number(quote.low)) ? Number(quote.low).toFixed(2) : "—"}</span>
                <span>昨收 {Number.isFinite(Number(quote.pre_close)) ? Number(quote.pre_close).toFixed(2) : "—"}</span>
                <span>
                  换手{" "}
                  {Number.isFinite(Number(quote.turnover_rate))
                    ? `${(quote.turnover_rate * 100).toFixed(2)}%`
                    : "—"}
                </span>
                <span>来源 {quote.source || "—"}</span>
              </div>
            </div>
          ) : null}
        </div>
        <p className="rt-foot">数据仅供研究参考，实时性以各数据源为准；市场有风险，决策需谨慎。</p>
      </div>
    </div>
  );
}
