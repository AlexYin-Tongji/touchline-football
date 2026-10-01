import { useCallback, useEffect, useRef, useState } from "react";
import { LoaderCircle, Play, Square } from "lucide-react";
import { api, errorText, post } from "./api";
import { SourceGapNotice } from "./components";
import type { SourceGap } from "./types";

export interface WorkflowState {
  kind: "news" | "team";
  team_id: string;
  enabled: boolean;
  status: "idle" | "running" | "completed" | "partial" | "stopping" | "stopped" | "interrupted" | "blocked" | "failed";
  phase: string;
  message: string;
  last_error: string | null;
  source_errors?: SourceGap[];
  collection?: {
    status: "ready" | "partial" | "failed" | "empty";
    reason: string;
    indexed_count: number;
    relevant_count: number;
    dated_count: number;
    window_count: number;
    read_attempted: number;
    readable_count: number;
    source_count: number;
    available_sources: number;
    failed_sources: number;
  } | null;
  started_at: string | null;
  finished_at: string | null;
  next_run_at: string | null;
  updated_at: string | null;
  progress: { completed: number; failed: number; remaining: number };
  memory: { summary: string; next_steps: string[]; completed_count: number };
}

const isActive = (state: WorkflowState | null) => !!state &&
  (state.enabled || state.status === "running" || state.status === "stopping");
const messages: Record<WorkflowState["status"], string> = {
  idle: "点击启动，更新一次资料。",
  running: "正在处理本轮资料，已完成的内容会陆续显示。",
  completed: "本次更新已完成。",
  partial: "本次部分工作未完成，已有内容保留，可手动重试。",
  stopping: "正在结束当前工作，已有内容会保留。",
  stopped: "已结束，已有内容保留。",
  interrupted: "上次跟进已中断，点击启动继续。",
  blocked: "暂时无法继续，请查看下方说明。",
  failed: "本轮未能完成，请查看下方说明。",
};

export function WorkflowControls({ kind, teamId, onChange, onConfigure }: {
  kind: WorkflowState["kind"];
  teamId: string;
  onChange?: (state: WorkflowState) => void;
  onConfigure?: () => void;
}) {
  const target = `${kind}/${teamId}`;
  const endpoint = `/workflows/${kind}/${encodeURIComponent(teamId)}`;
  const [state, setState] = useState<WorkflowState | null>(null);
  const [error, setError] = useState<{ target: string; message: string } | null>(null);
  const [pending, setPending] = useState<{ target: string; action: "start" | "stop" } | null>(null);
  const mounted = useRef(false);
  const activeTarget = useRef(target);
  const generation = useRef(0);
  const serial = useRef(0);
  const reading = useRef(false);
  const acting = useRef(false);
  const latest = useRef<WorkflowState | null>(null);
  const signature = useRef<string | null>(null);
  const onChanged = useRef(onChange);
  activeTarget.current = target;
  onChanged.current = onChange;

  const valid = useCallback((epoch: number, request: number) => mounted.current &&
    activeTarget.current === target && generation.current === epoch && serial.current === request, [target]);
  const accept = useCallback((next: WorkflowState) => {
    if (next.kind !== kind || next.team_id !== teamId) return;
    const nextSignature = JSON.stringify([next.enabled, next.status, next.phase, next.progress,
      next.finished_at, next.memory.completed_count, next.collection]);
    const changed = signature.current !== null && signature.current !== nextSignature;
    signature.current = nextSignature;
    latest.current = next;
    setState(next);
    if (changed) onChanged.current?.(next);
  }, [kind, teamId]);
  const load = useCallback(async () => {
    if (!mounted.current || activeTarget.current !== target || acting.current || reading.current) return;
    const epoch = generation.current;
    const request = ++serial.current;
    reading.current = true;
    try {
      const next = await api<WorkflowState>(endpoint);
      if (!valid(epoch, request)) return;
      accept(next);
      setError(null);
    } catch (err) {
      if (valid(epoch, request)) setError({ target, message: errorText(err) });
    } finally {
      if (valid(epoch, request)) reading.current = false;
    }
  }, [target, endpoint, valid, accept]);

  useEffect(() => {
    mounted.current = true;
    generation.current++;
    serial.current++;
    reading.current = false;
    acting.current = false;
    latest.current = null;
    signature.current = null;
    setState(null);
    setError(null);
    setPending(null);
    void load();
    return () => {
      mounted.current = false;
      generation.current++;
      serial.current++;
    };
  }, [load]);

  const current = state?.kind === kind && state.team_id === teamId ? state : null;
  const active = isActive(current);
  useEffect(() => {
    const refreshVisible = () => {
      if (document.visibilityState === "visible") void load();
    };
    const timer = active ? window.setInterval(refreshVisible, 4000) : null;
    document.addEventListener("visibilitychange", refreshVisible);
    return () => {
      if (timer !== null) clearInterval(timer);
      document.removeEventListener("visibilitychange", refreshVisible);
    };
  }, [active, load]);

  const control = async (action: "start" | "stop") => {
    if (!mounted.current || activeTarget.current !== target || acting.current) return;
    if (action === "start" ? isActive(latest.current) : !isActive(latest.current)) return;
    const epoch = generation.current;
    const request = ++serial.current;
    // A poll started before this command cannot undo its returned state.
    reading.current = false;
    acting.current = true;
    setPending({ target, action });
    setError(null);
    try {
      const next = await post<WorkflowState>(`${endpoint}/${action}`);
      if (valid(epoch, request)) accept(next);
    } catch (err) {
      if (valid(epoch, request)) setError({ target, message: errorText(err) });
    } finally {
      if (valid(epoch, request)) {
        acting.current = false;
        setPending(null);
      }
    }
  };

  const action = pending?.target === target ? pending.action : null;
  const requestError = error?.target === target ? error.message : "";
  const ready = !!current || !!requestError;
  const progress = current?.progress;
  const showProgress = progress && (progress.completed + progress.failed + progress.remaining > 0);
  const collection = current && current.status !== "idle" && current.status !== "stopped" ? current.collection : null;
  // Source collection can fail before a report is counted. The backend clears
  // this error at each new cycle; idle/stopped records may retain older errors.
  const failure = current && current.status !== "idle" && current.status !== "stopped"
    ? current.last_error || "" : "";
  const sourceErrors = current && current.status !== "idle" && current.status !== "stopped"
    ? current.source_errors || [] : [];
  return (
    <section className="workflow-controls" aria-label={kind === "news" ? "手动更新动态" : "手动更新球队资料"}>
      <div className="workflow-status" role="status" aria-live="polite">
        <strong>{action === "start" ? "正在启动…" : action === "stop" ? "正在结束…" :
          current?.message || (current ? messages[current.status] : requestError ? "暂时无法读取跟进状态。" : "正在读取跟进状态…")}</strong>
        {collection && <p>本轮来源：{collection.available_sources} / {collection.source_count} 个索引可用，发现 {collection.relevant_count} 条本队消息，{collection.window_count} 条在近期范围；尝试读取 {collection.read_attempted} 条正文，取得 {collection.readable_count} 条。</p>}
        {showProgress && <p>{kind === "news" ? "中文整理" : "本轮资料"}：已完成 {progress.completed}，待处理 {progress.remaining}{progress.failed > 0 ? `，未完成 ${progress.failed}` : ""}</p>}
        {(requestError || failure) && <p className="workflow-error" role="alert">
          {requestError || failure}
          {kind === "news" && onConfigure && /模型|配置/.test(requestError || failure) &&
            <button className="text-button" onClick={onConfigure}>模型设置</button>}
        </p>}
        {!!sourceErrors.length && <details className="workflow-source-errors">
          <summary>查看 {sourceErrors.length} 个未能读取的来源</summary>
          <SourceGapNotice gaps={sourceErrors} />
        </details>}
      </div>
      <div className="workflow-actions">
        <button className="button primary" disabled={!ready || active || !!action} onClick={() => control("start")}>
          {action === "start" ? <LoaderCircle size={15} className="spin" /> : <Play size={15} />}启动
        </button>
        <button className="button" disabled={!ready || !active || !!action || current?.status === "stopping"} onClick={() => control("stop")}>
          {action === "stop" ? <LoaderCircle size={15} className="spin" /> : <Square size={15} />}结束
        </button>
      </div>
    </section>
  );
}
