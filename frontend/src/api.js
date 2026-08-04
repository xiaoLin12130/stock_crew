// API 客户端：fetch 封装 + 离线 mock（仅 DEV 后端不可用时自动降级；结构对齐 §五 契约）
// 也可用 URL 参数 ?offline=1 强制走 mock 演示（无后端时可离线演示全流程）。
import {
  ANALYSTS,
  buildChatMeta,
  buildMockJobResult,
  buildMockReviewDetail,
  isoDate,
  mockChatReply,
  mockChatSessionList,
  mockReviewList,
  nowTime,
  seedMockChatSessions,
  seedMockReviews,
} from "./mock.js";

export class ApiError extends Error {
  constructor(message) {
    super(message);
    this.name = "ApiError";
  }
}

const REQUEST_TIMEOUT_MS = 30000;
// 聊天消息：多分析师交叉为串行 LLM 调用（每轮最长 120s），允许最长 4 分钟
const CHAT_MESSAGE_TIMEOUT_MS = 240000;

// 带超时的 fetch：杜绝无限卡死（AbortController 中止后抛中文超时错误）
async function fetchWithTimeout(path, options = {}, timeoutMs = REQUEST_TIMEOUT_MS) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await fetch(path, { ...options, signal: controller.signal });
  } catch (err) {
    if (err && err.name === "AbortError") {
      throw new ApiError(`请求超时（${Math.round(timeoutMs / 1000)} 秒），请检查后端服务后重试`);
    }
    throw err;
  } finally {
    clearTimeout(timer);
  }
}

async function errorFrom(res, fallback) {
  let detail = "";
  try {
    const data = await res.json();
    detail = (data && (data.detail || data.message || data.error)) || "";
  } catch {
    /* 非 JSON 错误体 */
  }
  return new ApiError(detail || `${fallback}（HTTP ${res.status}）`);
}

async function requestJson(path, options) {
  let res;
  try {
    res = await fetchWithTimeout(path, options);
  } catch (err) {
    if (err instanceof ApiError) throw err; // 超时等已带中文信息
    throw new ApiError("无法连接后端服务，请确认服务已启动");
  }
  if (!res.ok) throw await errorFrom(res, "请求失败");
  if (res.status === 204) return undefined;
  return res.json();
}

function forceOffline() {
  try {
    return new URLSearchParams(window.location.search).get("offline") === "1";
  } catch {
    return false;
  }
}

// 仅 DEV 降级；生产环境报错不静默
function canFallback() {
  return import.meta.env.DEV || forceOffline();
}

// ---------- 离线 mock 存储（结构完全对齐契约） ----------
const mockReviews = seedMockReviews();
const mockChatSessions = seedMockChatSessions();
const mockJobs = new Map();
let mockSeq = 0;

// ---------- 复盘 ----------
// POST /api/reviews {date, mode, max_rounds?} → {job_id}
export async function startReview(payload) {
  try {
    const res = await fetchWithTimeout("/api/reviews", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!res.ok) throw await errorFrom(res, "提交复盘任务失败");
    return await res.json();
  } catch (err) {
    if (canFallback()) return startOfflineJob(payload);
    if (err instanceof ApiError) throw err;
    throw new ApiError("无法连接后端服务，请确认服务已启动");
  }
}

// GET /api/jobs/{job_id}
export function getJob(jobId) {
  if (mockJobs.has(jobId)) return Promise.resolve(pollMockJob(jobId));
  return requestJson(`/api/jobs/${encodeURIComponent(jobId)}`);
}

// GET /api/data/realtime — 盘中实时数据快照（指数/涨跌停/板块/快讯）
export async function getRealtimeData() {
  try {
    return await requestJson("/api/data/realtime");
  } catch (err) {
    if (canFallback()) return buildMockRealtime();
    throw err;
  }
}

// GET /api/reviews（历史分组列表）
export async function listReviews() {
  try {
    const data = await requestJson("/api/reviews");
    return { data, offline: false };
  } catch (err) {
    if (canFallback()) return { data: mockReviewList(mockReviews), offline: true };
    throw err;
  }
}

// GET /api/reviews/{date}/{time}
export function getReviewDetail(date, time) {
  const key = `${date}/${time}`;
  const mock = mockReviews.get(key);
  if (mock) return Promise.resolve(mock);
  return requestJson(`/api/reviews/${encodeURIComponent(date)}/${encodeURIComponent(time)}`);
}

// DELETE /api/reviews/{date}/{time} → 204 / 404（中文）
export async function deleteReview(date, time) {
  const key = `${date}/${time}`;
  if (mockReviews.has(key)) {
    mockReviews.delete(key);
    return undefined;
  }
  let res;
  try {
    res = await fetchWithTimeout(`/api/reviews/${encodeURIComponent(date)}/${encodeURIComponent(time)}`, {
      method: "DELETE",
    });
  } catch (err) {
    if (canFallback()) return undefined;
    if (err instanceof ApiError) throw err;
    throw new ApiError("无法连接后端服务，请确认服务已启动");
  }
  if (!res.ok) throw await errorFrom(res, "删除复盘记录失败");
  return undefined; // 204
}

// ---------- 聊天 ----------
// POST /api/chat/sessions
export async function createChatSession(payload) {
  try {
    const res = await fetchWithTimeout("/api/chat/sessions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!res.ok) throw await errorFrom(res, "创建会话失败");
    return await res.json();
  } catch (err) {
    if (canFallback()) return createOfflineChatSession(payload);
    if (err instanceof ApiError) throw err;
    throw new ApiError("无法连接后端服务，请确认服务已启动");
  }
}

// POST /api/chat/sessions/{id}/messages {content} → {session_id, messages[], disclaimer}
export async function sendChatMessage(sessionId, content) {
  const session = mockChatSessions.find((s) => s.session_id === sessionId);
  if (session) {
    const newMessages = mockChatReply(session.meta, content);
    session.messages.push(...newMessages);
    return { session_id: sessionId, messages: newMessages, disclaimer: session.disclaimer, offline: true };
  }
  try {
    const res = await fetchWithTimeout(
      `/api/chat/sessions/${encodeURIComponent(sessionId)}/messages`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ content }),
      },
      CHAT_MESSAGE_TIMEOUT_MS
    );
    if (!res.ok) throw await errorFrom(res, "发送消息失败");
    return await res.json();
  } catch (err) {
    if (canFallback()) {
      return { session_id: sessionId, messages: [], disclaimer: "仅供参考，不构成投资建议", offline: true };
    }
    if (err instanceof ApiError) throw err;
    throw new ApiError("无法连接后端服务，请确认服务已启动");
  }
}

// SSE 流式聊天：POST /api/chat/sessions/{id}/messages/stream
// onEvent(event, payload)：meta / user_msg / analyst_start / analyst_delta / analyst_end /
//                         summary_start / summary_delta / summary_end / done / error
export async function streamChatMessage(sessionId, content, onEvent) {
  const session = mockChatSessions.find((s) => s.session_id === sessionId);
  if (session) return mockStreamChatMessage(session, content, onEvent);

  let res;
  try {
    res = await fetch(`/api/chat/sessions/${encodeURIComponent(sessionId)}/messages/stream`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content }),
    });
  } catch (err) {
    if (canFallback()) return mockStreamChatMessage(sessionId, content, onEvent);
    throw new ApiError("无法连接后端服务，请确认服务已启动");
  }
  if (!res.ok) throw await errorFrom(res, "发送消息失败");
  if (!res.body) throw new ApiError("后端不支持流式响应，请刷新后重试");

  const reader = res.body.getReader();
  const decoder = new TextDecoder("utf-8");
  let buf = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let idx;
    while ((idx = buf.indexOf("\n\n")) >= 0) {
      const block = buf.slice(0, idx);
      buf = buf.slice(idx + 2);
      let ev = "message";
      let data = "";
      for (const line of block.split("\n")) {
        if (line.startsWith("event:")) ev = line.slice(6).trim();
        else if (line.startsWith("data:")) data = line.slice(5).trim();
      }
      if (data) {
        let payload;
        try {
          payload = JSON.parse(data);
        } catch {
          payload = { raw: data };
        }
        onEvent(ev, payload);
      }
    }
  }
}

// 离线 mock：模拟逐块流式事件（结构与后端 SSE 一致）
function mockStreamChatMessage(session, content, onEvent) {
  const reply = mockChatReply(session.meta, content);
  const [userMsg, assistantMsg] = reply;
  const sid = typeof session === "string" ? session : session.session_id;
  const meta = typeof session === "string" ? { target_type: "stock", target: "mock", analysts: [] } : session.meta;
  onEvent("meta", { session_id: sid });
  onEvent("user_msg", userMsg);
  if (typeof session !== "string") session.messages.push(userMsg);
  const text = assistantMsg.content || "";
  const step = Math.max(3, Math.ceil(text.length / 25));
  const chunks = [];
  for (let i = 0; i < text.length; i += step) chunks.push(text.slice(i, i + step));
  const name = assistantMsg.analyst_name || (meta.analysts && meta.analysts[0]) || "分析师";
  onEvent("analyst_start", { index: 0, name });
  return new Promise((resolve) => {
    let i = 0;
    const timer = setInterval(() => {
      if (i < chunks.length) {
        onEvent("analyst_delta", { index: 0, name, delta: chunks[i] });
        i += 1;
      } else {
        clearInterval(timer);
        onEvent("analyst_end", { index: 0, name, content: text });
        onEvent("summary_start", {});
        onEvent("summary_end", { content: "" });
        if (typeof session !== "string") {
          session.messages.push(assistantMsg);
          onEvent("done", {
            session_id: sid,
            messages: session.messages,
            disclaimer: session.disclaimer || "仅供参考，不构成投资建议",
          });
        } else {
          onEvent("done", {
            session_id: sid,
            messages: [userMsg, assistantMsg],
            disclaimer: "仅供参考，不构成投资建议",
          });
        }
        resolve();
      }
    }, 40);
  });
}

// GET /api/chat/sessions?target=&date=
export async function listChatSessions(filters = {}) {
  try {
    const q = new URLSearchParams();
    if (filters.target) q.set("target", String(filters.target));
    if (filters.date) q.set("date", String(filters.date));
    const suffix = q.toString() ? `?${q.toString()}` : "";
    const data = await requestJson(`/api/chat/sessions${suffix}`);
    return { data, offline: false };
  } catch (err) {
    if (canFallback()) return { data: mockChatSessionList(mockChatSessions, filters), offline: true };
    throw err;
  }
}

// GET /api/chat/sessions/{id} → {meta, messages[], disclaimer}
export function getChatSession(id) {
  const session = mockChatSessions.find((s) => s.session_id === id);
  if (session) return Promise.resolve(session);
  return requestJson(`/api/chat/sessions/${encodeURIComponent(id)}`);
}

// DELETE /api/chat/sessions/{id} → 204 / 404（中文）
export async function deleteChatSession(id) {
  const idx = mockChatSessions.findIndex((s) => s.session_id === id);
  if (idx >= 0) {
    mockChatSessions.splice(idx, 1);
    return undefined;
  }
  let res;
  try {
    res = await fetchWithTimeout(`/api/chat/sessions/${encodeURIComponent(id)}`, { method: "DELETE" });
  } catch (err) {
    if (canFallback()) return undefined;
    if (err instanceof ApiError) throw err;
    throw new ApiError("无法连接后端服务，请确认服务已启动");
  }
  if (!res.ok) throw await errorFrom(res, "删除会话失败");
  return undefined;
}

// ---------- 离线 mock 任务（模拟进度，仅 DEV 降级使用） ----------
const MOCK_STEPS = [
  { stage: "fetch", pct: 8, msg: "正在模式化取数（指数/涨跌停/板块/情绪…）…" },
  { stage: "fetch", pct: 16, msg: "取数完成：主要数据块就绪，竞价块走降级链标注" },
  { stage: "news", pct: 24, msg: "正在解析财经资讯与经济日历…" },
  { stage: "trend", pct: 32, done: 1, msg: "趋势派 1/2 · 老周 完成点评" },
  { stage: "trend", pct: 40, done: 2, msg: "趋势派 2/2 · 小林 完成点评" },
  { stage: "sentiment", pct: 50, done: 1, msg: "情绪派 1/3 · 阿凯 完成点评" },
  { stage: "sentiment", pct: 58, done: 2, msg: "情绪派 2/3 · 陈姐 完成点评" },
  { stage: "sentiment", pct: 66, done: 3, msg: "情绪派 3/3 · 大熊 完成点评" },
  { stage: "host", pct: 76, msg: "主持人正在汇总各方观点…" },
  { stage: "debate", pct: 86, msg: "辩论第 1/2 轮进行中…" },
  { stage: "debate", pct: 92, msg: "辩论第 2/2 轮完成，意见收敛" },
  { stage: "report", pct: 97, msg: "正在生成 Markdown 报告（强制附加免责声明）…" },
  { stage: "done", pct: 100, msg: "复盘完成" },
];

function startOfflineJob(payload) {
  const jobId = `mock-${Date.now()}-${mockSeq++}`;
  mockJobs.set(jobId, { step: -1, payload: { ...payload } });
  return { job_id: jobId, offline: true };
}

function pollMockJob(jobId) {
  const job = mockJobs.get(jobId);
  if (!job) throw new ApiError("演示任务不存在");
  job.step += 1;
  const idx = Math.min(job.step, MOCK_STEPS.length - 1);
  const s = MOCK_STEPS[idx];
  if (idx === MOCK_STEPS.length - 1) {
    if (!job.recordId) {
      const payload = { ...job.payload, time: nowTime() };
      const detail = buildMockReviewDetail(payload);
      const result = buildMockJobResult(payload);
      job.recordId = result.record_id;
      mockReviews.set(`${detail.meta.date}/${detail.meta.time}`, detail);
      job.result = result;
    }
    return {
      job_id: jobId,
      status: "done",
      stage: "done",
      pct: 100,
      message: "复盘完成",
      analysts_done: 5,
      analysts_total: 5,
      error: null,
      result: job.result,
      offline: true,
    };
  }
  return {
    job_id: jobId,
    status: "running",
    stage: s.stage,
    pct: s.pct,
    message: s.msg,
    analysts_done: s.done || 0,
    analysts_total: 5,
    error: null,
    result: null,
    offline: true,
  };
}

function createOfflineChatSession(payload) {
  // 会话 ID 对齐后端契约："{YYYY-MM-DD}_{HHMMSS}"（同一秒冲突顺延 1 秒，对齐 I3 storage）
  const pad2 = (x) => String(x).padStart(2, "0");
  let sessionId = `${isoDate()}_${nowTime()}`;
  let guard = 0;
  while (mockChatSessions.some((s) => s.session_id === sessionId) && guard < 60) {
    guard += 1;
    const d = new Date();
    d.setSeconds(d.getSeconds() + guard);
    sessionId = `${isoDate()}_${pad2(d.getHours())}${pad2(d.getMinutes())}${pad2(d.getSeconds())}`;
  }
  const meta = buildChatMeta(payload);
  const session = {
    session_id: sessionId,
    meta,
    messages: [],
    disclaimer: "仅供参考，不构成投资建议",
  };
  // 创建后自动生成一条欢迎回复（多分析师交叉演示）
  const welcome = mockChatReply(meta, "请先给出一段开场分析");
  session.messages.push(...welcome);
  mockChatSessions.push(session);
  return { session_id: sessionId, offline: true };
}

export { ANALYSTS };
