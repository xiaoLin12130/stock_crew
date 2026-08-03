// normalize 层：严格对照 requirements §五 API 契约做字段映射
// 约定：比率一律小数（0.5=50%，展示层 ×100）；None=null → 前端「—」（禁 0）；
//       日期 YYYY-MM-DD；时间点 HHMMSS；响应恒含 disclaimer；degraded[] 可见标注。
//       补充（I4/I5 对齐）：record_id / session_id = "{date}_{time}"（复盘时间点可带 -N 后缀）；
//       created_at 为无时区本地 ISO "YYYY-MM-DDTHH:mm:ss"（后端 datetime.now().isoformat(timespec="seconds")）。
import { num } from "./format.js";

export const arr = (v) => (Array.isArray(v) ? v : []);
const n = (v, d = 0) => num(v, d);
const nullable = (v) => num(v, NaN);
const str = (v, d = "") => (v == null || v === "" ? d : String(v));

export const DEFAULT_DISCLAIMER = "仅供参考，不构成投资建议";

// ---- GET /api/health ----
export function normalizeHealth(raw = {}) {
  return { status: str(raw.status, "unknown") };
}

// ---- GET /api/jobs/{job_id} ----
export function normalizeJob(job = {}) {
  const j = job && typeof job === "object" ? job : {};
  const status = str(j.status, "queued");
  return {
    job_id: str(j.job_id),
    status,
    stage: str(j.stage, "queued"),
    pct: n(j.pct),
    message: str(j.message),
    analysts_done: n(j.analysts_done),
    analysts_total: n(j.analysts_total),
    error: str(j.error),
    result: j.result || null,
    offline: !!j.offline,
    isRunning: status === "queued" || status === "running",
    isDone: status === "done",
    isError: status === "error",
  };
}

// ---- meta（嵌套字段）----
export function normalizeMeta(meta = {}) {
  const m = meta && typeof meta === "object" ? meta : {};
  const degraded = arr(m.degraded).filter((x) => x != null && x !== "");
  return {
    date: str(m.date),
    mode: str(m.mode),
    mode_label: str(m.mode_label),
    time: str(m.time),
    created_at: str(m.created_at),
    degraded,
    disclaimer: str(m.disclaimer) || DEFAULT_DISCLAIMER,
    summary: str(m.summary),
    isDegraded: degraded.length > 0,
  };
}

// ---- 分析师（skill_name / analysis / suggestion / tags）----
export function normalizeAnalyst(x = {}, i = 0) {
  const a = x && typeof x === "object" ? x : {};
  return {
    skill_name: str(a.skill_name || a.name, `分析师${i + 1}`),
    skill_id: str(a.skill_id || a.id),
    analysis: str(a.analysis || a.content || a.report || a.comment),
    suggestion: str(a.suggestion),
    tags: arr(a.tags),
  };
}

// ---- 辩论记录 [{round, topic, responses:[{skill_name, response}]}] ----
export function normalizeDebateHistory(raw) {
  return arr(raw)
    .map((d, i) => {
      if (!d || typeof d !== "object") return null;
      if (Array.isArray(d.responses) || d.round != null || d.topic != null) {
        return {
          round: n(d.round) || i + 1,
          topic: str(d.topic),
          responses: arr(d.responses).map((r) => ({
            skill_name: str(r && (r.skill_name || r.name || r.speaker)),
            response: str(r && (r.response || r.point || r.content)),
          })),
        };
      }
      return {
        round: i + 1,
        topic: "",
        responses: [
          {
            skill_name: str(d.speaker || d.name),
            response: str(d.point || d.content),
          },
        ],
      };
    })
    .filter(Boolean);
}

const ladderRow = (x) => ({
  label: str(x && x.label),
  count: nullable(x && x.count),
  stocks: arr(x && x.stocks),
});

// ---- snapshot（图表数据快照，比率一律小数）----
export function normalizeSnapshot(raw = {}) {
  const s = raw && typeof raw === "object" ? raw : {};
  return {
    index_minute: arr(s.index_minute)
      .map((ix) => ({
        name: str(ix && ix.name),
        points: arr(ix && ix.points)
          .map((p) => ({
            time: str(p && p.time),
            value: nullable(p && p.value),
          }))
          .filter((p) => p.time),
      }))
      .filter((ix) => ix.name),
    limit_ladder: {
      up: arr(s.limit_ladder && s.limit_ladder.up).map(ladderRow),
      down: arr(s.limit_ladder && s.limit_ladder.down).map(ladderRow),
    },
    sectors: arr(s.sectors).map((x) => ({
      name: str(x && x.name),
      pct_change: nullable(x && x.pct_change),
      leader: str(x && x.leader),
    })),
    sentiment: {
      up_count: nullable(s.sentiment && s.sentiment.up_count),
      down_count: nullable(s.sentiment && s.sentiment.down_count),
      limit_up_count: nullable(s.sentiment && s.sentiment.limit_up_count),
      limit_down_count: nullable(s.sentiment && s.sentiment.limit_down_count),
      red_rate: nullable(s.sentiment && s.sentiment.red_rate),
      continue_rate: nullable(s.sentiment && s.sentiment.continue_rate),
      break_rate: nullable(s.sentiment && s.sentiment.break_rate),
      button_rate: nullable(s.sentiment && s.sentiment.button_rate),
      avg_return: nullable(s.sentiment && s.sentiment.avg_return),
      up_down_ratio: nullable(s.sentiment && s.sentiment.up_down_ratio),
      source: str(s.sentiment && s.sentiment.source),
      degraded: arr(s.sentiment && s.sentiment.degraded),
    },
    source: str(s.source),
    degraded: arr(s.degraded),
  };
}

// ---- 报告详情（GET /api/reviews/{date}/{time} 与 job result 共用部分）----
export function normalizeReport(raw = {}) {
  const r = raw && typeof raw === "object" ? raw : {};
  const meta = normalizeMeta(r.meta);
  const degraded = arr(r.degraded && r.degraded.length ? r.degraded : meta.degraded);
  return {
    meta,
    report: str(r.report || r.final_report),
    analyses: arr(r.analyses || r.analyst_reports).map((x, i) => normalizeAnalyst(x, i)),
    debate_history: normalizeDebateHistory(r.debate_history || r.debate),
    snapshot: normalizeSnapshot(r.snapshot),
    disclaimer: str(r.disclaimer) || meta.disclaimer || DEFAULT_DISCLAIMER,
    degraded,
    isDegraded: degraded.length > 0,
  };
}

// ---- job result（{record_id, meta, report, snapshot}）----
export function normalizeJobResult(raw = {}) {
  const r = raw && typeof raw === "object" ? raw : {};
  const meta = normalizeMeta(r.meta);
  return {
    record_id: str(r.record_id),
    meta,
    report: str(r.report || r.final_report),
    snapshot: normalizeSnapshot(r.snapshot),
    disclaimer: meta.disclaimer || DEFAULT_DISCLAIMER,
  };
}

// ---- GET /api/reviews（历史分组列表）----
export function normalizeReviewList(raw) {
  return arr(raw)
    .map((g) => ({
      date: str(g && g.date),
      items: arr(g && g.items).map((it) => ({
        record_id: str(it && it.record_id),
        mode: str(it && it.mode),
        mode_label: str(it && it.mode_label),
        time: str(it && it.time),
        created_at: str(it && it.created_at),
        summary: str(it && it.summary),
      })),
    }))
    .filter((g) => g.date);
}

// ---- GET /api/reviews/{date}/{time} ----
export function normalizeReviewDetail(raw = {}) {
  return normalizeReport(raw);
}

// ---- 聊天 ----
export function normalizeChatMessage(m = {}, i = 0) {
  const x = m && typeof m === "object" ? m : {};
  return {
    id: str(x.id, `m${i}`),
    role: str(x.role, "assistant"),
    content: str(x.content),
    analyst_name: str(x.analyst_name),
    analyst_parts: arr(x.analyst_parts).map((p) => ({
      skill_name: str(p && (p.skill_name || p.name)),
      content: str(p && (p.content || p.response)),
    })),
    created_at: str(x.created_at),
    isUser: String(x.role) === "user",
    isMulti: arr(x.analyst_parts).length > 1,
  };
}

function normalizeChatAnalysts(list) {
  return arr(list).map((a) => (typeof a === "string" ? a : str(a && a.skill_name)));
}

export function normalizeChatSession(raw = {}) {
  const s = raw && typeof raw === "object" ? raw : {};
  const meta = s.meta || {};
  return {
    meta: {
      session_id: str(s.session_id || meta.session_id),
      target_type: str(meta.target_type),
      target: str(meta.target),
      target_name: str(meta.target_name || meta.target),
      analysts: normalizeChatAnalysts(meta.analysts),
      title: str(meta.title),
      created_at: str(meta.created_at),
      date: str(meta.date),
    },
    messages: arr(s.messages).map((x, i) => normalizeChatMessage(x, i)),
    disclaimer: str(s.disclaimer) || DEFAULT_DISCLAIMER,
  };
}

export function normalizeChatSessionList(raw) {
  return arr(raw).map((s) => {
    const x = s && typeof s === "object" ? s : {};
    const meta = x.meta || {};
    return {
      session_id: str(x.session_id || meta.session_id),
      target_type: str(x.target_type || meta.target_type),
      target: str(x.target || meta.target),
      target_name: str(x.target_name || meta.target_name || x.target || meta.target),
      analysts: normalizeChatAnalysts(x.analysts || meta.analysts),
      title: str(x.title || meta.title),
      created_at: str(x.created_at || meta.created_at),
      date: str(x.date || meta.date),
      last_message: str(x.last_message),
      message_count: n(x.message_count),
    };
  });
}
