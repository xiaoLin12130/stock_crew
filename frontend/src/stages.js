// 复盘任务阶段（requirements §三：取数/资讯/趋势派 n/2/情绪派 n/3/主持人/辩论/报告/完成）
export const STAGES = [
  { key: "fetch", label: "取数" },
  { key: "news", label: "资讯" },
  { key: "trend", label: "趋势派" },
  { key: "sentiment", label: "情绪派" },
  { key: "host", label: "主持人" },
  { key: "debate", label: "辩论" },
  { key: "report", label: "报告" },
  { key: "done", label: "完成" },
];

export function stageIndex(stage) {
  const s = String(stage || "").toLowerCase();
  if (s.includes("fetch") || s.includes("data") || s.includes("取数")) return 0;
  if (s.includes("news") || s.includes("info") || s.includes("资讯")) return 1;
  if (s.includes("trend") || s.includes("趋势")) return 2;
  if (s.includes("sentiment") || s.includes("情绪")) return 3;
  if (s.includes("host") || s.includes("moderator") || s.includes("主持")) return 4;
  if (s.includes("debat") || s.includes("辩论")) return 5;
  if (s.includes("report") || s.includes("报告")) return 6;
  if (s.includes("done") || s.includes("finish") || s.includes("完成")) return 7;
  return 0;
}

export function stageLabel(stage) {
  const s = String(stage || "").toLowerCase();
  if (s === "queued" || s.includes("queue") || s.includes("提交") || s.includes("排队")) {
    return "排队等待";
  }
  return STAGES[Math.min(stageIndex(stage), STAGES.length - 1)].label;
}

// 趋势派/情绪派阶段显示「n/总数」（如 趋势派 1/2）
export function stageStepLabel(stage, done, total) {
  const s = String(stage || "").toLowerCase();
  if ((s.includes("trend") || s.includes("趋势")) && total) return `趋势派 ${done}/${total}`;
  if ((s.includes("sentiment") || s.includes("情绪")) && total) return `情绪派 ${done}/${total}`;
  return stageLabel(stage);
}
