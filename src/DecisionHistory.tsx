import { useEffect, useRef, useState } from "react";
import { api, errorText } from "./api";

type Part = "roster" | "schedule" | "standings" | "portraits";
type Change = {
  field: Part;
  changed?: boolean;
  confirmed: boolean;
  last_confirmed_at?: string | null;
};
type Decision = {
  id: string;
  created_at: string;
  finished_at?: string;
  trigger: "automatic" | "manual";
  status: string;
  reason: string;
  summary?: string;
  season_label?: string | null;
  parts: Part[];
  changes?: Change[];
};
type History = { items: Decision[]; history_note?: string };
type State = { teamId: string; items: Decision[]; note: string; loading: boolean; error: string };

const partNames: Record<Part, string> = {
  roster: "阵容", schedule: "赛程", standings: "积分", portraits: "球员照片",
};
const statusNames: Record<string, string> = {
  running: "更新中", completed: "已完成", partial: "部分完成", failed: "更新失败",
  interrupted: "已中断", checked: "已检查", deferred: "稍后再试",
};

function time(value?: string | null) {
  if (!value || !Number.isFinite(Date.parse(value))) return "时间待确认";
  return new Date(value).toLocaleString("zh-CN", {
    year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
  });
}

function changeText(change: Change) {
  const name = partNames[change.field] || "资料";
  const result = change.changed === true ? "有变化" : change.changed === false ? "无变化" : "变化未展示";
  return `${name}：${change.confirmed ? `已重新确认，${result}` : "本轮未重新确认"}`;
}

export function DecisionHistory({ teamId, version }: { teamId: string; version?: string }) {
  const [retry, setRetry] = useState(0);
  const [state, setState] = useState<State>({ teamId, items: [], note: "", loading: true, error: "" });
  const request = useRef(0);
  const active = useRef(teamId);
  active.current = teamId;

  useEffect(() => {
    const serial = ++request.current;
    setState({ teamId, items: [], note: "", loading: true, error: "" });
    void api<History>(`/teams/${encodeURIComponent(teamId)}/decisions?limit=5`).then((result) => {
      if (serial !== request.current || active.current !== teamId) return;
      setState({ teamId, items: Array.isArray(result.items) ? result.items : [],
        note: result.history_note || "", loading: false, error: "" });
    }).catch((error: unknown) => {
      if (serial !== request.current || active.current !== teamId) return;
      setState({ teamId, items: [], note: "", loading: false, error: errorText(error) });
    });
    return () => { request.current++; };
  }, [teamId, retry, version]);

  const current = state.teamId === teamId ? state : { teamId, items: [], note: "", loading: true, error: "" };
  return <details className="decision-history" onToggle={(event) => {
    if (event.currentTarget.open) setRetry((value) => value + 1);
  }}>
    <summary>更新记录<span>{current.loading ? "读取中" : current.error ? "暂不可用" : `${current.items.length} 条`}</span></summary>
    <div className="decision-history-body">
      {current.loading && <p>正在读取更新记录…</p>}
      {current.error && <p role="alert">更新记录暂不可读：{current.error} <button type="button" onClick={() => setRetry((value) => value + 1)}>重试</button></p>}
      {!current.loading && !current.error && !current.items.length &&
        <p>{current.note || "此功能启用后才会留下更新记录，之前的观察无法补回。"}</p>}
      {!current.loading && !current.error && current.items.map((item) =>
        <article key={item.id} className="decision-entry">
          <div className="decision-entry-head">
            <strong>{item.trigger === "automatic" ? "自动更新" : "手动更新"} · {statusNames[item.status] || "已记录"}</strong>
            <time dateTime={item.created_at}>{time(item.created_at)}</time>
          </div>
          <p>{item.reason}</p>
          {item.summary && <p>{item.summary}</p>}
          <small>{item.season_label ? `${item.season_label} 赛季 · ` : ""}{item.parts.map((part) => partNames[part] || "资料").join("、")}</small>
          {item.finished_at && <small>完成于 {time(item.finished_at)}</small>}
          {!!item.changes?.length && <ul>{item.changes.map((change) =>
            <li key={change.field}>{changeText(change)}{change.last_confirmed_at && <span> · 最近资料时间 {time(change.last_confirmed_at)}</span>}</li>
          )}</ul>}
        </article>)}
    </div>
  </details>;
}
