import React, { useCallback, useEffect, useRef, useState } from "react";
import * as api from "./api";
import {
  normalizeJob,
  normalizeJobResult,
  normalizeReviewDetail,
  normalizeReviewList,
  normalizeChatSessionList,
} from "./normalize";
import { modeLabel } from "./modes";
import ErrorBoundary from "./components/ErrorBoundary";
import Sidebar from "./components/Sidebar";
import HomeView from "./components/HomeView";
import ProgressView from "./components/ProgressView";
import ReportView from "./components/ReportView";
import ChatView from "./components/ChatView";

export default function App() {
  const [collapsed, setCollapsed] = useState(() => {
    try {
      return localStorage.getItem("stock-review.sidebarCollapsed") === "1";
    } catch {
      return false;
    }
  });
  const [sidebarOpen, setSidebarOpen] = useState(false); // 移动端抽屉
  // 视图：home | progress | report | chat
  const [view, setView] = useState("home");
  // 运行中任务全局保留：切换视图不停止轮询、不丢失任务
  const [job, setJob] = useState(null);
  const [report, setReport] = useState(null);
  const [reportLoading, setReportLoading] = useState(false);
  const [reportError, setReportError] = useState(null);
  // 历史复盘
  const [history, setHistory] = useState({ groups: [], loading: true, error: null, offline: false });
  // 聊天
  const [chatSessions, setChatSessions] = useState([]);
  const [chatSessionsError, setChatSessionsError] = useState("");
  const [activeChatSessionId, setActiveChatSessionId] = useState(null);
  const [chatIntent, setChatIntent] = useState(null);

  const pollRef = useRef(null);
  const viewRef = useRef(view);

  useEffect(() => {
    viewRef.current = view;
  }, [view]);

  const stopPolling = useCallback(() => {
    if (pollRef.current) {
      clearInterval(pollRef.current);
      pollRef.current = null;
    }
  }, []);

  const loadHistory = useCallback(async () => {
    setHistory((h) => ({ ...h, loading: true, error: null }));
    try {
      const res = await api.listReviews();
      setHistory({
        groups: normalizeReviewList(res.data),
        loading: false,
        error: null,
        offline: !!(res && res.offline),
      });
    } catch (err) {
      setHistory({ groups: [], loading: false, error: err.message || "历史记录加载失败", offline: false });
    }
  }, []);

  const loadChatSessions = useCallback(async () => {
    try {
      const res = await api.listChatSessions();
      setChatSessions(normalizeChatSessionList(res.data));
      setChatSessionsError("");
    } catch (err) {
      setChatSessionsError((err && err.message) || "会话列表加载失败");
    }
  }, []);

  useEffect(() => {
    loadHistory();
    loadChatSessions();
  }, [loadHistory, loadChatSessions]);

  useEffect(() => () => stopPolling(), [stopPolling]);

  // 「新建复盘」（侧栏唯一入口）：只切换视图，进行中的任务继续轮询
  const handleNew = useCallback(() => {
    setSidebarOpen(false);
    setReport(null);
    setReportError(null);
    setChatIntent(null);
    setView("home");
  }, []);

  const beginPolling = useCallback(
    (jobId, metaLabel, offlineFlag) => {
      stopPolling();
      setJob((prev) => ({ ...(prev || {}), id: jobId, metaLabel, offline: offlineFlag }));
      pollRef.current = setInterval(async () => {
        try {
          const snap = await api.getJob(jobId);
          const norm = normalizeJob(snap);
          setJob((prev) => ({ ...(prev || {}), ...norm, metaLabel }));
          if (norm.isDone || norm.isError) {
            stopPolling();
            if (norm.isDone) {
              const fresh = normalizeJobResult(norm.result);
              setReport({ ...fresh, analyses: [], debate_history: [], offline: offlineFlag });
              if (viewRef.current === "progress") setView("report");
              loadHistory();
              // 详情接口补全分析师点评与辩论记录（失败不阻塞报告展示）
              api
                .getReviewDetail(fresh.meta.date, fresh.meta.time)
                .then((raw) => {
                  const detail = normalizeReviewDetail(raw);
                  setReport((prev) =>
                    prev && prev.record_id === fresh.record_id
                      ? { ...prev, analyses: detail.analyses, debate_history: detail.debate_history }
                      : prev
                  );
                })
                .catch(() => {});
            }
          }
        } catch (err) {
          stopPolling();
          setJob((prev) => ({
            ...(prev || {}),
            status: "error",
            error: err.message || "轮询任务状态失败",
          }));
        }
      }, 1000);
    },
    [stopPolling, loadHistory]
  );

  const handleStartReview = useCallback(
    async (payload) => {
      setReport(null);
      setReportError(null);
      setJob({
        id: null,
        metaLabel: modeLabel(payload.mode),
        status: "queued",
        stage: "queued",
        pct: 0,
        message: "正在提交复盘任务…",
        analysts_done: 0,
        analysts_total: 0,
        offline: false,
      });
      setView("progress");
      try {
        const res = await api.startReview(payload);
        beginPolling(res.job_id, modeLabel(payload.mode), !!res.offline);
      } catch (err) {
        setJob((prev) => ({ ...(prev || {}), status: "error", error: err.message || "提交失败" }));
      }
    },
    [beginPolling]
  );

  // 运行中点击历史记录：只切换展示内容，轮询继续
  const handleOpenReview = useCallback(
    async (date, time) => {
      setReportError(null);
      setReportLoading(true);
      setView("report");
      try {
        const raw = await api.getReviewDetail(date, time);
        const detail = normalizeReviewDetail(raw);
        setReport({
          ...detail,
          record_id: `${date}_${time}`,
          offline: false,
        });
      } catch (err) {
        setReportError((err && err.message) || "复盘详情加载失败");
        setReport(null);
      } finally {
        setReportLoading(false);
      }
    },
    []
  );

  const handleDeleteReview = useCallback(
    async (date, time) => {
      await api.deleteReview(date, time);
      await loadHistory();
      if (
        report &&
        report.meta &&
        report.meta.date === date &&
        report.meta.time === time
      ) {
        setReport(null);
        setView("home");
      }
    },
    [loadHistory, report]
  );

  // 侧栏指示器：进行中 → 回到进度；已完成 → 查看结果；失败 → 查看原因
  const handleBackToJob = useCallback(() => {
    if (!job) return;
    if (job.status === "done" && report) {
      setView("report");
      return;
    }
    setView("progress");
  }, [job, report]);

  const toggleCollapsed = useCallback(() => {
    setCollapsed((c) => {
      const next = !c;
      try {
        localStorage.setItem("stock-review.sidebarCollapsed", next ? "1" : "0");
      } catch {
        /* 忽略存储异常 */
      }
      return next;
    });
  }, []);

  // 聊天
  const handleOpenChat = useCallback((payload) => {
    setChatIntent(payload);
    setView("chat");
  }, []);

  const handleSelectChatSession = useCallback((id) => {
    setChatIntent(null);
    setActiveChatSessionId(id);
    setView("chat");
  }, []);

  const handleDeleteChatSession = useCallback(
    async (id) => {
      await api.deleteChatSession(id);
      if (activeChatSessionId === id) setActiveChatSessionId(null);
      await loadChatSessions();
    },
    [activeChatSessionId, loadChatSessions]
  );

  const handleSessionCreated = useCallback(
    (id) => {
      setActiveChatSessionId(id);
      loadChatSessions();
    },
    [loadChatSessions]
  );

  const activeReviewKey =
    report && report.meta && report.meta.date
      ? `${report.meta.date}/${report.meta.time}`
      : null;

  const title =
    view === "progress" && job
      ? "复盘进行中"
      : view === "report"
        ? report
          ? `复盘报告 · ${report.meta.mode_label || ""}`
          : "复盘报告"
        : view === "chat"
          ? "分析师对话"
          : "新建复盘";

  return (
    <ErrorBoundary onReset={handleNew}>
      <div className="app">
        {sidebarOpen ? (
          <div className="sidebar-backdrop" onClick={() => setSidebarOpen(false)} />
        ) : null}
        <Sidebar
          collapsed={collapsed}
          open={sidebarOpen}
          onCloseMobile={() => setSidebarOpen(false)}
          onToggle={toggleCollapsed}
          history={history}
          activeReviewKey={activeReviewKey}
          onSelectReview={(k) => { setSidebarOpen(false); handleOpenReview(k); }}
          onNew={() => { setSidebarOpen(false); handleNew(); }}
          job={job}
          onBackToJob={() => { setSidebarOpen(false); handleBackToJob(); }}
          chatSessions={chatSessions}
          activeChatId={activeChatSessionId}
          onOpenChatSession={(id) => { setSidebarOpen(false); handleSelectChatSession(id); }}
          onDeleteReview={handleDeleteReview}
          onDeleteChatSession={handleDeleteChatSession}
        />
        <main className="main">
          <header className="topbar">
            <div className="topbar-title">
              <button
                className="menu-btn"
                aria-label="打开菜单"
                onClick={() => setSidebarOpen((o) => !o)}
              >
                ☰
              </button>
              {title}
              {history.offline ? <span className="pill offline topbar-badge">离线 mock</span> : null}
            </div>
            {job && (job.isRunning || job.isDone || job.isError) ? (
              <button className="btn btn-outline btn-sm" onClick={handleBackToJob}>
                {job.isRunning ? `任务进行中 ${Math.round(job.pct || 0)}%` : job.isDone ? "查看结果" : "查看失败原因"}
              </button>
            ) : null}
          </header>
          <div className="content">
            {reportLoading ? (
              <div className="card loading-card">
                <div className="spinner" />
                <div>正在加载复盘报告…</div>
              </div>
            ) : reportError ? (
              <div className="card error-panel">
                <div className="error-icon">⚠️</div>
                <div className="error-title">加载失败</div>
                <div className="error-detail">{reportError}</div>
                <div className="error-actions">
                  <button className="btn btn-outline" onClick={loadHistory}>刷新历史记录</button>
                  <button className="btn btn-accent" onClick={handleNew}>返回首页</button>
                </div>
              </div>
            ) : view === "progress" && job ? (
              <ProgressView job={job} onNew={handleNew} />
            ) : view === "report" && report ? (
              <ReportView report={report} offline={!!report.offline} />
            ) : view === "chat" ? (
              <ChatView
                sessions={chatSessions}
                sessionsLoading={false}
                sessionsError={chatSessionsError}
                activeSessionId={activeChatSessionId}
                intent={chatIntent}
                onIntentConsumed={() => setChatIntent(null)}
                onSelectSession={handleSelectChatSession}
                onRefreshSessions={loadChatSessions}
                onSessionCreated={handleSessionCreated}
              />
            ) : (
              <HomeView
                onStartReview={handleStartReview}
                onOpenReview={handleOpenReview}
                onDeleteReview={handleDeleteReview}
                history={history}
                onRetryHistory={loadHistory}
                onOpenChat={handleOpenChat}
              />
            )}
          </div>
        </main>
      </div>
    </ErrorBoundary>
  );
}
