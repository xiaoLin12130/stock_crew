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
    const res = await fetchWithTimeout(`/api/chat/sessions/${encodeURIComponent(sessionId)}/messages`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content }),
    });
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
