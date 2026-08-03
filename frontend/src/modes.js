// 6 种复盘时间模式（requirements §二）
export const MODES = [
  {
    code: "pre_market",
    label: "早盘前决策",
    window: "当日 09:15 前",
    desc: "隔夜外盘、昨夜消息、昨日收盘复盘 → 今日大盘预判与操作计划",
  },
  {
    code: "auction",
    label: "竞价复盘",
    window: "09:15–09:25",
    desc: "竞价高开/低开、竞价金额、抢筹/砸盘、热门股异动 → 早盘操作建议",
  },
  {
    code: "intraday_am",
    label: "上午盘中",
    window: "09:30–11:30",
    desc: "上午分时、实时板块涨幅/资金、涨停/炸板 → 上午回顾与午后判断",
  },
  {
    code: "noon",
    label: "午间复盘",
    window: "11:30–13:00",
    desc: "上午全量分时、板块涨幅/资金、涨停梯队 → 下午走势判断与买卖计划",
  },
  {
    code: "intraday_pm",
    label: "下午盘中",
    window: "13:00–15:00",
    desc: "全天分时、板块资金流、涨停梯队/情绪指标 → 尾盘策略与明日预案",
  },
  {
    code: "close",
    label: "收盘复盘",
    window: "15:00 后",
    desc: "全天行情：指数/涨跌停/板块/情绪/资金/龙虎榜/资讯 → 当日报告与明日计划",
  },
];

export const DEFAULT_MODE = "close";

// 各模式适用窗口（分钟制），[start, end)
const WINDOW = {
  pre_market: [0, 9 * 60 + 15],
  auction: [9 * 60 + 15, 9 * 60 + 25],
  intraday_am: [9 * 60 + 30, 11 * 60 + 30],
  noon: [11 * 60 + 30, 13 * 60],
  intraday_pm: [13 * 60, 15 * 60],
  close: [15 * 60, 24 * 60],
};

export function modeByCode(code) {
  return MODES.find((m) => m.code === code) || null;
}

export function modeLabel(code) {
  const m = modeByCode(code);
  return m ? m.label : code || "—";
}

export function nowMinutes() {
  const d = new Date();
  return d.getHours() * 60 + d.getMinutes();
}

export function todayStr() {
  const d = new Date();
  const p = (x) => String(x).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}

export function isTradingDay(dateStr) {
  const d = new Date(`${dateStr}T12:00:00`);
  if (Number.isNaN(d.getTime())) return true;
  const day = d.getDay();
  return day !== 0 && day !== 6;
}

// 当前时间点最合适的模式（规则 1：建议最近可用模式）
export function currentMode(minutes) {
  if (minutes < 9 * 60 + 15) return "pre_market";
  if (minutes < 9 * 60 + 30) return "auction"; // 09:15–09:30（含竞价刚结束的缓冲段）
  if (minutes < 11 * 60 + 30) return "intraday_am";
  if (minutes < 13 * 60) return "noon";
  if (minutes < 15 * 60) return "intraday_pm";
  return "close";
}

// 前端窗口提示（服务端也会判定）：所选时间点不在模式窗口内 → 中文提示并建议最近可用模式
export function windowHints(modeCode, dateStr, minutes = nowMinutes()) {
  const m = modeByCode(modeCode);
  if (!m) return [];
  const hints = [];
  const isToday = dateStr === todayStr();
  if (!isToday) return hints; // 历史日期补做任意模式，不提示
  if (!isTradingDay(dateStr)) {
    hints.push("今天是周末（非交易日），盘中数据源可能无当日数据，复盘将按降级链标注缺失。");
  }
  const [start, end] = WINDOW[modeCode];
  const inWindow = minutes >= start && minutes < end;
  if (!inWindow) {
    if (modeCode === "auction" && minutes >= 9 * 60 + 25 && minutes < 9 * 60 + 30) {
      hints.push("当前处于 09:25–09:30，竞价数据刚结束，建议切换为「竞价复盘」或「上午盘中」。");
    } else {
      const cur = currentMode(minutes);
      const reason = minutes < start ? "尚未到适用时间" : "该模式数据窗口已结束";
      hints.push(
        `当前时间不在「${m.label}」窗口内（${m.window}，${reason}），建议切换为「${modeLabel(cur)}」。`
      );
    }
  }
  if (modeCode === "intraday_am" && minutes < 10 * 60) {
    hints.push("盘中模式执行建议：上午盘中建议 10:00 后运行（可选，不强制）。");
  }
  if (modeCode === "intraday_pm" && minutes > 14 * 60 && minutes < 15 * 60) {
    hints.push("盘中模式执行建议：下午盘中建议 14:00 前运行（可选，不强制）。");
  }
  return hints;
}
