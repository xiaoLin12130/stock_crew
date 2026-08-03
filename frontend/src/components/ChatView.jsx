import React, { useEffect, useMemo, useRef, useState } from "react";
import * as api from "../api";
import { normalizeChatMessage, normalizeChatSession } from "../normalize";
import { fmtDate, fmtDateTime } from "../format";

export default function ChatView({
  sessions,
  sessionsLoading,
  sessionsError,
  activeSessionId,
  intent,
  onIntentConsumed,
  onSelectSession,
  onRefreshSessions,
  onSessionCreated,
}) {
  const [filterTarget, setFilterTarget] = useState("");
  const [filterDate, setFilterDate] = useState("");
  const [session, setSession] = useState(null); // 归一化后的会话 {meta, messages, disclaimer}
  const [sessionLoading, setSessionLoading] = useState(false);
  const [sessionError, setSessionError] = useState("");
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  const [sendError, setSendError] = useState("");
  const createdRef = useRef(null);
  const scrollRef = useRef(null);

  const filtered = useMemo(() => {
    const t = filterTarget.trim();
    const d = filterDate.trim();
    return sessions.filter((s) => {
      const tOk = !t || (s.target || "").includes(t) || (s.target_name || "").includes(t);
      const dOk = !d || s.date === d;
      return tOk && dOk;
    });
  }, [sessions, filterTarget, filterDate]);

  // 从首页聊天卡片进入：创建会话
  useEffect(() => {
    if (!intent) return;
    const key = JSON.stringify(intent);
    if (createdRef.current === key) return;
    createdRef.current = key;
    let cancelled = false;
    (async () => {
      setSessionError("");
      setSessionLoading(true);
      try {
        const res = await api.createChatSession(intent);
        if (cancelled) return;
        onIntentConsumed();
        onSessionCreated(res.session_id);
      } catch (err) {
        if (cancelled) return;
        setSessionError((err && err.message) || "创建会话失败，请重试");
        setSessionLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [intent, onIntentConsumed, onSessionCreated]);

  // 切换会话：加载消息
  useEffect(() => {
    if (!activeSessionId) {
      setSession(null);
      setSessionError("");
      return;
    }
    let cancelled = false;
    setSessionLoading(true);
    setSessionError("");
    api
      .getChatSession(activeSessionId)
      .then((raw) => {
        if (cancelled) return;
        setSession(normalizeChatSession(raw));
        setSessionLoading(false);
      })
      .catch((err) => {
        if (cancelled) return;
        setSessionError((err && err.message) || "加载会话失败");
        setSessionLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [activeSessionId]);

  useEffect(() => {
    if (scrollRef.current) scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
  }, [session && session.messages.length]);

  const handleSend = async () => {
    const text = input.trim();
    if (!text || !session || sending) return;
    setSending(true);
    setSendError("");
    try {
      const res = await api.sendChatMessage(session.meta.session_id, text);
      const newMsgs = (Array.isArray(res.messages) ? res.messages : []).map((m, i) => normalizeChatMessage(m, i));
      setSession((prev) => {
        if (!prev) return prev;
        const messages =
          newMsgs.length > prev.messages.length
            ? newMsgs // 后端返回全量消息
            : [...prev.messages, ...newMsgs];
        return {
          ...prev,
          messages,
          disclaimer: res.disclaimer || prev.disclaimer,
        };
      });
      setInput("");
      onRefreshSessions();
    } catch (err) {
      setSendError((err && err.message) || "发送失败，请重试");
    } finally {
      setSending(false);
    }
  };

  const active = session ? session.meta : sessions.find((s) => s.session_id === activeSessionId) || null;

  return (
    <div className="chat-layout">
      {/* 会话列表 */}
      <div className="chat-sessions">
        <div className="chat-sessions-head">
          <div className="card-title">会话列表</div>
          <div className="chat-filters">
            <input
              className="search-input"
              placeholder="按标的过滤"
              value={filterTarget}
              onChange={(e) => setFilterTarget(e.target.value)}
            />
            <input
              className="search-input"
              type="date"
              value={filterDate}
              onChange={(e) => setFilterDate(e.target.value)}
            />
          </div>
        </div>
        <div className="chat-session-scroll">
          {sessionsLoading ? (
            <div className="sidebar-empty">正在加载会话…</div>
          ) : sessionsError ? (
            <div className="sidebar-empty">
              {sessionsError}
              <button className="btn btn-ghost btn-xs" style={{ marginTop: 8 }} onClick={onRefreshSessions}>
                重试
              </button>
            </div>
          ) : filtered.length === 0 ? (
            <div className="sidebar-empty">暂无匹配会话，可从首页聊天卡片发起新对话</div>
          ) : (
            filtered.map((s) => (
              <div
                key={s.session_id}
                role="button"
                tabIndex={0}
                className={`chat-session-item${activeSessionId === s.session_id ? " active" : ""}`}
                onClick={() => onSelectSession(s.session_id)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" || e.key === " ") {
                    e.preventDefault();
                    onSelectSession(s.session_id);
                  }
                }}
              >
                <div className="chat-session-title">{s.title || `${s.target_name || s.target} · 对话`}</div>
                <div className="chat-session-meta">
                  <span className="tag-chip light">{s.target_type === "sector" ? "板块" : "个股"}</span>
                  <span className="zero">{s.analysts.length} 位分析师 · {s.message_count} 条</span>
                  <span className="hist-time">{fmtDateTime(s.created_at)}</span>
                </div>
              </div>
            ))
          )}
        </div>
      </div>

      {/* 消息流 */}
      <div className="chat-main">
        {sessionLoading || (!session && activeSessionId) ? (
          <div className="loading-card">
            <div className="spinner" />
            <div style={{ marginTop: 10 }}>正在加载会话消息…</div>
          </div>
        ) : sessionError ? (
          <div className="error-panel">
            <div className="error-icon">⚠️</div>
            <div className="error-title">会话加载失败</div>
            <div className="error-detail">{sessionError}</div>
          </div>
        ) : !active ? (
          <div className="chat-empty">
            <div className="chat-empty-icon">💬</div>
            <div>选择左侧会话查看对话，或返回首页聊天卡片发起新对话</div>
          </div>
        ) : (
          <>
            <div className="chat-head">
              <div>
                <div className="chat-head-title">{active.title || `${active.target_name} · 分析师对话`}</div>
                <div className="chat-head-sub">
                  {active.target_type === "sector" ? "板块" : "个股"} · {active.target_name || active.target}
                  {active.date ? ` · ${fmtDate(active.date)}` : ""}
                </div>
              </div>
              <div className="chat-head-analysts">
                {(active.analysts && active.analysts.length ? active.analysts : []).map((name) => (
                  <span key={name} className="tag-chip light">{name}</span>
                ))}
              </div>
            </div>

            <div className="msg-scroll" ref={scrollRef}>
              {session.messages.length === 0 ? (
                <div className="empty">暂无消息，输入问题开始对话</div>
              ) : (
                session.messages.map((m) =>
                  m.isUser ? (
                    <div key={m.id} className="msg user">
                      <div className="msg-bubble">{m.content}</div>
                    </div>
                  ) : m.isMulti ? (
                    <div key={m.id} className="msg multi">
                      <div className="msg-multi-head">🤝 多位分析师交叉回复</div>
                      {m.analyst_parts.map((p, i) => (
                        <div key={i} className="msg-part">
                          <span className="msg-part-name">{p.skill_name || `分析师 ${i + 1}`}</span>
                          <div className="msg-part-content">{p.content}</div>
                        </div>
                      ))}
                      <div className="msg-summary">
                        <b>汇总</b>
                        <div>{m.content}</div>
                      </div>
                    </div>
                  ) : (
                    <div key={m.id} className="msg">
                      <div className="msg-bubble">
                        {m.analyst_name ? <div className="msg-analyst-name">{m.analyst_name}</div> : null}
                        {m.content}
                      </div>
                    </div>
                  )
                )
              )}
            </div>

            {sendError ? <div className="upload-error">{sendError}</div> : null}
            {sending ? (
              <div className="chat-hint">
                分析师正在思考…（多分析师交叉为串行推理，通常约 1-3 分钟，请勿重复发送）
              </div>
            ) : null}
            <div className="chat-input-row">
              <input
                className="chat-input"
                placeholder="输入问题，Enter 发送（内容仅供研究参考）"
                value={input}
                disabled={sending}
                onChange={(e) => setInput(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && !e.nativeEvent.isComposing) {
                    e.preventDefault();
                    handleSend();
                  }
                }}
              />
              <button className="btn btn-accent" disabled={sending || !input.trim()} onClick={handleSend}>
                {sending ? "回复中…" : "发送"}
              </button>
            </div>
            <div className="chat-disclaimer">
              <span>⚠️</span>
              <span>免责声明：{session.disclaimer || "仅供参考，不构成投资建议"}。市场有风险，入市需谨慎。</span>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
