// 格式化工具：全中文展示口径（数据字典 §六）
export function num(v, fallback = 0) {
  if (v === null || v === undefined || v === "") return fallback;
  const n = typeof v === "number" ? v : Number(v);
  return Number.isFinite(n) ? n : fallback;
}

export function isNone(v) {
  if (v === null || v === undefined || v === "") return true;
  const n = typeof v === "number" ? v : Number(v);
  return !Number.isFinite(n);
}

// None = 无数据 → 「—」（禁止 0）
export function fmtNone(v) {
  return isNone(v) ? "—" : String(v);
}

// 比率一律小数存储、前端 ×100 展示（0.5 = 50%）
export function fmtPct(v, { digits = 2, signed = false } = {}) {
  const n = num(v, NaN);
  if (!Number.isFinite(n)) return "—";
  const sign = signed && n > 0 ? "+" : "";
  return `${sign}${(n * 100).toFixed(digits)}%`;
}

export function fmtSignedPct(v, digits = 2) {
  return fmtPct(v, { digits, signed: true });
}

// 红涨绿跌：正红 #E03131、负绿 #0CA678、零中性灰
export function pctClass(v) {
  const n = num(v, NaN);
  if (!Number.isFinite(n) || n === 0) return "zero";
  return n > 0 ? "pos" : "neg";
}

export function moneyClass(v) {
  const n = num(v, NaN);
  if (!Number.isFinite(n)) return "";
  if (n === 0) return "zero";
  return n > 0 ? "pos" : "neg";
}

export function fmtMoney(v, digits = 2) {
  const n = num(v, NaN);
  if (!Number.isFinite(n)) return "—";
  return `¥${n.toLocaleString("zh-CN", { minimumFractionDigits: digits, maximumFractionDigits: digits })}`;
}

export function fmtCount(v) {
  const n = num(v, NaN);
  return Number.isFinite(n) ? n.toLocaleString("zh-CN") : "—";
}

// 月份统一「2025年11月」
export function fmtMonth(m) {
  if (!m) return "—";
  const match = String(m).trim().match(/^(\d{4})[-/](\d{1,2})/);
  if (match) return `${match[1]}年${Number(match[2])}月`;
  return String(m);
}

// 日期统一「2025-11-27」
export function fmtDate(d) {
  if (!d) return "—";
  const match = String(d).trim().match(/^(\d{4})-(\d{2})-(\d{2})/);
  return match ? `${match[1]}-${match[2]}-${match[3]}` : String(d).slice(0, 10);
}

// HHMMSS → HH:MM（历史「08:50 早盘前决策」）
export function fmtTimeHM(t) {
  if (!t) return "—";
  const m = String(t).trim().match(/^(\d{2})(\d{2})(\d{2})$/);
  if (m) return `${m[1]}:${m[2]}`;
  const s = String(t).trim();
  const iso = s.match(/T(\d{2}):(\d{2})/);
  if (iso) return `${iso[1]}:${iso[2]}`;
  return s.slice(0, 5);
}

export function fmtDateTime(ts) {
  if (!ts) return "—";
  const s = String(ts).trim();
  const compact = s.match(/^(\d{4})(\d{2})(\d{2})[-_/]?(\d{2})(\d{2})(\d{2})$/);
  if (compact) return `${compact[1]}-${compact[2]}-${compact[3]} ${compact[4]}:${compact[5]}`;
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return s.slice(0, 16);
  const p = (x) => String(x).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}
