import { useI18n } from "./i18n";
import { useEffect, useState } from "react";
import type { GumCodeState } from "./gumCodeTypes";

const toolLabels: Record<string, string> = {
  list_files: "查看目录", read_file: "读取文件", write_file: "写入文件", edit_file: "编辑文件",
  apply_patch: "应用修改", search_files: "搜索文件", search_content: "搜索内容", git_status: "检查 Git 状态",
  git_diff: "查看改动", todo_write: "更新计划", run_command: "执行命令", delete_file: "删除文件",
  web_search: "联网搜索", read_webpage: "读取网页",
  project_service: "操作项目服务", project_browser: "检查页面",
};
const displayLabel = (value: string, language: "zh" | "en") => value.split(" · ").map((part, index) => index === 0 ? language === "en" ? part.replaceAll("_", " ") : toolLabels[part] || part : part).join(" · ");

export function GumCodeProgress({ state, visible }: { state: GumCodeState; visible: boolean }) {
  const { t, language } = useI18n();
  const [now, setNow] = useState(Date.now);
  const [expanded, setExpanded] = useState(false);
  const active = state.status !== "idle";
  useEffect(() => {
    setNow(Date.now());
    if (!active || !visible) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [active, visible, state.turnStartedAt]);
  useEffect(() => { setExpanded(false); }, [state.turnStartedAt]);
  const progress = state.progress || [];
  const plan = state.plan || [];
  if (!state.turnStartedAt && !active) return null;
  const seconds = state.turnStartedAt ? Math.max(0, Math.floor(((state.turnEndedAt || now) - state.turnStartedAt) / 1000)) : 0;
  const duration = seconds < 60 ? `${seconds} ${t("秒", "sec")}` : `${Math.floor(seconds / 60)} ${t("分", "min")} ${seconds % 60} ${t("秒", "sec")}`;
  const label = active ? state.statusText || t("正在处理", "Processing") : state.error ? t("本轮失败", "Turn failed") : state.statusText || t("本轮结束", "Turn ended");
  return <section className="gum-code-progress" aria-label={t("执行过程", "Execution progress")}>
    <button type="button" className="gum-code-progress-toggle" aria-label={`${active ? t(displayLabel(label, language)) : `${t("用时", "Duration")} ${duration}`}，${expanded ? t("收起", "Collapse") : t("展开", "Expand")} ${t("执行过程", "Execution progress")}`} aria-expanded={expanded} onClick={() => setExpanded(!expanded)}>
      <span className="gum-code-progress-current" role="status">
        {active && <span className="gum-code-wait-dots" aria-hidden="true"><i /><i /><i /></span>}
        <span>{active || state.error || state.statusText !== "本轮已结束" ? t(displayLabel(label, language)) : `${t("用时", "Duration")} ${duration}`}</span>
      </span>
      <span className="gum-code-progress-time">{active && duration}<span className={`gum-code-progress-chevron${expanded ? " is-open" : ""}`} aria-hidden="true">›</span></span>
    </button>
    {expanded && <div className="gum-code-progress-body">
      {!plan.length && !progress.length && <p>{t(displayLabel(label, language))}</p>}
      {plan.length > 0 && <ol className="gum-code-plan" aria-label={t("任务计划", "Task plan")}>{plan.map(item => <li key={item.id} data-status={item.status}>
        <span aria-label={item.status === "completed" ? t("已完成", "Completed") : item.status === "running" ? t("进行中", "In progress") : t("待处理", "Pending")}>{item.status === "completed" ? "✓" : item.status === "running" ? "◉" : "○"}</span>{item.text}
      </li>)}</ol>}
      {progress.length > 0 && <ol className="gum-code-tool-list" aria-label={t("工具执行记录", "Tool execution log")}>{progress.map(item => <li key={item.id} data-status={item.status}>
        <span className="gum-code-tool-mark" aria-label={item.status === "completed" ? t("已完成", "Completed") : item.status === "failed" ? t("失败", "Failed") : t("进行中", "In progress")}>{item.status === "completed" ? "✓" : item.status === "failed" ? "!" : "◉"}</span>
        <div className="gum-code-tool-content"><strong title={item.label}>{displayLabel(item.label, language)}</strong>{item.detail && <span className="gum-code-tool-detail">{item.detail}</span>}</div>
      </li>)}</ol>}
    </div>}
  </section>;
}
