// 契约断言脚本：node scripts/verify-normalize.mjs
// 验证 src/normalize.js 严格对照 requirements §五 API 契约，
// 并以离线 mock 数据（mock.js）作为后端 Schema 样本做往返断言：
//   比率一律小数（0.5=50%，展示 ×100）；None → 「—」；日期 YYYY-MM-DD；
//   时间点 HHMMSS；meta.degraded[]；响应恒含 disclaimer；聊天逐位片段+汇总。
import assert from "node:assert/strict";
import {
  normalizeChatMessage,
  normalizeChatSession,
  normalizeJob,
  normalizeJobResult,
  normalizeReviewDetail,
  normalizeReviewList,
  normalizeSnapshot,
} from "../src/normalize.js";
import { fmtCount, fmtMonth, fmtPct, fmtTimeHM, pctClass } from "../src/format.js";
import { MODES, modeLabel } from "../src/modes.js";
import {
  buildMockJobResult,
  buildMockReviewDetail,
  mockChatReply,
  mockChatSessionList,
  mockReviewList,
  seedMockChatSessions,
  seedMockReviews,
} from "../src/mock.js";

const DATE = "2026-08-03";

// ---- §五：比率 ×100 / None → — / 月份中文 / HHMMSS ----
assert.equal(fmtPct(0.5), "50.00%", "比率 0.5 必须展示为 50.00%");
assert.equal(fmtPct(-0.123), "-12.30%");
assert.equal(fmtPct(null), "—", "None 必须展示为 —，禁止 0");
assert.equal(fmtPct(undefined), "—");
assert.equal(fmtCount(null), "—");
assert.equal(pctClass(null), "zero");
assert.equal(pctClass(0), "zero");
assert.equal(pctClass(0.01), "pos");
assert.equal(pctClass(-0.01), "neg");
assert.equal(fmtMonth("2025-11"), "2025年11月");
assert.equal(fmtMonth("2025-11-27"), "2025年11月");
assert.equal(fmtTimeHM("150500"), "15:05");

// ---- §五：6 模式中文标签 ----
assert.deepEqual(
  MODES.map((m) => m.code),
  ["pre_market", "auction", "intraday_am", "noon", "intraday_pm", "close"],
  "6 模式代码必须齐全"
);
assert.equal(modeLabel("pre_market"), "早盘前决策");
assert.equal(modeLabel("close"), "收盘复盘");

// ---- Job 轮询（running）字段齐备 ----
const runningJob = {
  job_id: "mock-test-1",
  status: "running",
  stage: "sentiment",
  pct: 58,
  message: "情绪派 2/3 · 陈姐 完成点评",
  analysts_done: 2,
  analysts_total: 5,
  result: null,
  error: null,
  offline: true,
};
const j1 = normalizeJob(runningJob);
assert.equal(j1.job_id, "mock-test-1");
assert.equal(j1.status, "running");
assert.equal(j1.stage, "sentiment");
assert.equal(j1.pct, 58);
assert.equal(j1.analysts_done, 2);
assert.equal(j1.analysts_total, 5);
assert.equal(j1.result, null);
assert.equal(j1.isRunning, true);
assert.equal(j1.isDone, false);
assert.equal(j1.isError, false);

// 空对象兜底（禁止白屏）：缺失字段 → queued / 0 / 空串
const jEmpty = normalizeJob({});
assert.equal(jEmpty.status, "queued");
assert.equal(jEmpty.stage, "queued");
assert.equal(jEmpty.pct, 0);
assert.equal(jEmpty.isRunning, true);

// ---- Job result（done）：{record_id, meta, report, snapshot} ----
const jobResult = buildMockJobResult({ date: DATE, mode: "close" });
assert.match(
  jobResult.record_id,
  /^\d{4}-\d{2}-\d{2}_\d{6}(?:-\d+)?$/,
  "record_id = 日期_时间点（I4/I5 契约：_ 分隔，时间点可带 -N 后缀）"
);
const jr = normalizeJobResult(jobResult);
assert.match(jr.meta.date, /^\d{4}-\d{2}-\d{2}$/, "meta.date 必须为 YYYY-MM-DD");
assert.match(jr.meta.time, /^\d{6}$/, "meta.time 必须为 HHMMSS");
assert.match(
  jr.meta.created_at,
  /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$/,
  "created_at 必须为无时区本地 ISO（YYYY-MM-DDTHH:mm:ss）"
);
assert.ok(
  !jr.meta.created_at.includes("+") && !jr.meta.created_at.includes("Z"),
  "created_at 不得携带时区后缀（+08:00 / Z）"
);
assert.equal(jr.meta.mode, "close");
assert.equal(jr.meta.mode_label, "收盘复盘");
assert.ok(Array.isArray(jr.meta.degraded) && jr.meta.degraded.length > 0, "meta.degraded[] 必须可见标注");
assert.ok(jr.meta.disclaimer && jr.meta.disclaimer.includes("仅供参考"), "meta 恒含 disclaimer");
assert.ok(jr.report.length > 0 && jr.report.includes("#"), "report 必须为非空 Markdown");
assert.equal(jr.record_id, jobResult.record_id);

// ---- 历史分组列表：按日期倒序、同日多份按时间点倒序、HH:MM + 模式标签 ----
const reviewMap = seedMockReviews(new Date(`${DATE}T00:00:00`));
const list = mockReviewList(reviewMap);
assert.ok(list.length >= 2, "历史至少 2 个日期分组");
const dates = list.map((g) => g.date);
assert.deepEqual(dates, [...dates].sort((a, b) => (a < b ? 1 : -1)), "日期必须倒序");
const firstGroup = list[0];
assert.equal(firstGroup.date, DATE);
assert.ok(firstGroup.items.length >= 2, "同日多份（今日至少 2 份）");
const times = firstGroup.items.map((it) => it.time);
assert.deepEqual(times, [...times].sort((a, b) => (a < b ? 1 : -1)), "同日按时间点倒序");
for (const it of firstGroup.items) {
  assert.match(it.time, /^\d{6}$/);
  assert.match(it.record_id, /^\d{4}-\d{2}-\d{2}_\d{6}(?:-\d+)?$/, "列表 record_id 必须为 日期_时间点");
  assert.match(it.created_at, /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$/, "列表 created_at 必须为无时区本地 ISO");
  assert.ok(MODES.some((m) => m.code === it.mode), "item.mode 必须为 6 模式之一");
  assert.equal(it.mode_label, modeLabel(it.mode));
}
const nl = normalizeReviewList(list);
assert.equal(nl[0].date, DATE);
assert.equal(nl[0].items[0].mode_label, modeLabel(nl[0].items[0].mode));
assert.ok(nl[0].items[0].summary.length > 0);

// ---- 详情：analyses / debate_history / snapshot 严格对齐 ----
const detail = buildMockReviewDetail({ date: DATE, mode: "noon" });
const d = normalizeReviewDetail(detail);
assert.equal(d.meta.date, DATE);
assert.equal(d.meta.mode_label, "午间复盘");
assert.match(d.meta.created_at, /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$/, "详情 meta.created_at 必须为无时区本地 ISO");
assert.equal(d.analyses.length, 5, "5 位分析师（趋势派 2 + 情绪派 3）");
for (const a of d.analyses) {
  assert.ok(a.skill_name && a.skill_name.length > 0, "analyst.skill_name 必须存在");
  assert.ok(typeof a.analysis === "string" && a.analysis.length > 0, "analyst.analysis 必须存在");
  assert.ok(typeof a.suggestion === "string" && a.suggestion.length > 0, "analyst.suggestion 必须存在");
  assert.ok(Array.isArray(a.tags), "analyst.tags 必须为数组");
}
assert.ok(d.debate_history.length >= 1, "辩论记录至少 1 轮");
for (const r of d.debate_history) {
  assert.ok(Number.isInteger(r.round) && r.round >= 1);
  assert.ok(Array.isArray(r.responses) && r.responses.length >= 1);
  for (const resp of r.responses) {
    assert.ok(resp.skill_name, "辩论 response.skill_name 必须存在");
    assert.ok(resp.response, "辩论 response.response 必须存在");
  }
}
assert.ok(d.report.includes("#"), "详情 report 为非空 Markdown");
assert.ok(d.disclaimer.includes("仅供参考"), "详情恒含 disclaimer");

// snapshot：指数分时 / 涨跌停梯队 / 板块 / 情绪
const snap = d.snapshot;
assert.ok(snap.index_minute.length >= 1, "指数分时至少 1 条");
assert.ok(snap.index_minute[0].points.length >= 2, "分时至少 2 个点");
for (const p of snap.index_minute[0].points) {
  assert.match(p.time, /^\d{2}:\d{2}$/, "分时点 time 为 HH:MM");
  assert.ok(Number.isFinite(p.value), "分时点 value 必须为有限数");
}
assert.ok(snap.limit_ladder.up.length >= 2, "涨停梯队至少 2 档");
for (const row of [...snap.limit_ladder.up, ...snap.limit_ladder.down]) {
  assert.ok(row.label, "梯队 label 必须存在");
  assert.ok(Number.isFinite(row.count), "梯队 count 必须为有限数");
}
assert.ok(snap.sectors.length >= 5, "板块至少 5 条");
for (const s of snap.sectors) {
  assert.ok(s.name, "板块 name 必须存在");
  assert.ok(Number.isFinite(s.pct_change) && Math.abs(s.pct_change) < 1, "板块 pct_change 必须为小数（<100%）");
}
const st = snap.sentiment;
for (const key of ["red_rate", "continue_rate", "break_rate", "button_rate", "up_down_ratio"]) {
  assert.ok(Number.isFinite(st[key]) && st[key] > 0 && st[key] < 5, `情绪比率 ${key} 必须为小数`);
}
assert.ok(Number.isFinite(st.avg_return), "昨日涨停平均收益必须为小数");
assert.ok(Number.isInteger(st.up_count) && Number.isInteger(st.down_count));

// None 字段：normalize 保留 NaN，展示层 → —
const noneDetail = normalizeSnapshot({
  index_minute: [],
  limit_ladder: { up: [], down: [] },
  sectors: [],
  sentiment: { red_rate: null, break_rate: undefined, up_count: null, down_count: 0 },
});
assert.ok(Number.isNaN(noneDetail.sentiment.red_rate), "null 比率必须保留 NaN（禁 0）");
assert.equal(fmtPct(noneDetail.sentiment.red_rate), "—");
assert.equal(fmtCount(noneDetail.sentiment.up_count), "—");
assert.equal(noneDetail.sentiment.down_count, 0, "显式 0 不是 None，保留 0");
assert.deepEqual(noneDetail.index_minute, []);
assert.deepEqual(noneDetail.limit_ladder.up, []);

// ---- 聊天：会话结构 / 逐位片段 + 汇总 / 免责声明恒有 ----
const sessions = seedMockChatSessions(new Date(`${DATE}T00:00:00`));
assert.ok(sessions.length >= 2, "至少 2 个种子会话（多分析师 + 单分析师）");
const listRes = mockChatSessionList(sessions, {});
assert.ok(listRes.length >= 2);
const s1 = normalizeChatSession(sessions[0]);
assert.equal(s1.meta.target_type, "sector");
assert.match(s1.meta.session_id, /^\d{4}-\d{2}-\d{2}_\d{6}$/, "session_id 必须为 日期_时间点（I3 契约）");
assert.match(s1.meta.created_at, /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$/, "会话 created_at 必须为无时区本地 ISO");
assert.ok(s1.meta.analysts.length >= 2, "多分析师会话");
assert.ok(s1.disclaimer.includes("仅供参考"), "会话恒含 disclaimer");
const multi = s1.messages.find((m) => m.isMulti);
assert.ok(multi, "多分析师交叉回复必须存在");
assert.ok(multi.analyst_parts.length === s1.meta.analysts.length, "逐位片段数 = 分析师数");
assert.ok(multi.content.length > 0, "必须含汇总内容");
const s2 = normalizeChatSession(sessions[1]);
assert.equal(s2.meta.analysts.length, 1, "单分析师会话");
assert.match(s2.meta.session_id, /^\d{4}-\d{2}-\d{2}_\d{6}$/);
assert.ok(s2.messages.some((m) => !m.isUser), "单分析师直接回复");
for (const m of [...s1.messages, ...s2.messages]) {
  assert.match(m.created_at, /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$/, "消息 created_at 必须为无时区本地 ISO");
}

// 发送消息：逐位片段 + 汇总
const [userMsg, reply] = mockChatReply(sessions[0].meta, "明天怎么看？");
assert.equal(normalizeChatMessage(userMsg).isUser, true);
const r = normalizeChatMessage(reply);
assert.equal(r.isMulti, true);
assert.ok(r.analyst_parts.length >= 2);
assert.ok(r.content.length > 0);

// 按标的 / 日期过滤
const byTarget = mockChatSessionList(sessions, { target: "茅台" });
assert.equal(byTarget.length, 1);
const byDate = mockChatSessionList(sessions, { date: DATE });
assert.equal(byDate.length, sessions.length);

console.log("ALL NORMALIZE ASSERTIONS PASSED（mock 结构严格对齐 §五 契约）");
