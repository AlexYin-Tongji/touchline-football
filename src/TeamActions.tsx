import { useCallback, useEffect, useRef, useState } from "react";
import { Check, LoaderCircle, Play, Square } from "lucide-react";
import { api, errorText, post } from "./api";
import { friendlyTime } from "./shared";
import type { WorkflowState } from "./WorkflowControls";

export type SkillId =
  | "club.crest"
  | "squad.info"
  | "squad.names"
  | "squad.images"
  | "schedule.standings"
  | "stats.charts";

export type SkillTone = "running" | "fresh" | "gap" | "fail" | "idle";

export interface SkillRow {
  id: SkillId;
  label: string;
  tone: SkillTone;
  reason?: string;
  retryable?: boolean;
  detail?: string;
}

export interface PanelLeaf {
  id?: string;
  topic?: string;
  status?: string;
  reason_code?: string | null;
  retryable?: boolean;
  summary?: string;
  actionable?: boolean;
}

export interface ChartBlocks {
  status?: string;
  reason_code?: string | null;
  retryable?: boolean;
  sample?: {
    played?: number;
    from?: string | null;
    to?: string | null;
    season_id?: string | null;
    competition?: string | null;
    scope?: string | null;
    sources?: { name?: string; url?: string }[];
  } | null;
  results?: { won?: number; drawn?: number; lost?: number; played?: number } | null;
  goals?: { goals_for?: number; goals_against?: number; played?: number } | null;
  home_away?: {
    home?: { won?: number; drawn?: number; lost?: number; goals_for?: number; goals_against?: number; played?: number };
    away?: { won?: number; drawn?: number; lost?: number; goals_for?: number; goals_against?: number; played?: number };
  } | null;
}

export type PanelWorkflowState = WorkflowState & {
  leaves?: PanelLeaf[];
  skills?: PanelLeaf[];
  progress?: WorkflowState["progress"] & { settled?: number };
  panel?: { summary?: string; early_stop?: boolean; spawned?: string[]; serial?: string[] };
};

const SKILL_LABELS: Record<SkillId, string> = {
  "club.crest": "队徽与资料",
  "squad.info": "阵容",
  "squad.names": "中文名",
  "squad.images": "照片",
  "schedule.standings": "赛程/积分",
  "stats.charts": "数据图",
};

const SKILL_ORDER: SkillId[] = [
  "club.crest",
  "squad.info",
  "squad.names",
  "squad.images",
  "schedule.standings",
  "stats.charts",
];

const GAP_CODES = new Set([
  "MISSING", "STALE", "INCOMPLETE_SCHEDULE", "SOURCE_UNAVAILABLE", "RATE_LIMITED", "MODEL_FAILED",
]);
const FAIL_CODES = new Set([
  "NAMESAKE_BLOCKED", "CREST_MISSING", "SAMPLE_INSUFFICIENT", "SCOPE_DENIED", "INTERRUPTED",
]);

type ComponentPart = {
  status?: string;
  stale?: boolean;
  errors?: string[];
  note?: string;
  crest?: { status?: string; note?: string; missing?: string[] };
  available_count?: number;
  total_count?: number;
};

type CenterLike = {
  updated_at?: string;
  components?: Partial<Record<"roster" | "schedule" | "standings" | "portraits" | "profile", ComponentPart>>;
  coverage?: { has_full_schedule?: boolean };
  charts?: { goals_by_match?: unknown[]; results?: unknown[]; home_away?: unknown[] };
  chart_blocks?: ChartBlocks | null;
  stats?: { season?: { played?: number; won?: number; drawn?: number; lost?: number }; sample_complete?: boolean; hidden?: boolean };
  preparation?: { status?: string };
};

function toneFromLeaf(leaf: PanelLeaf | undefined, workflowRunning: boolean): SkillTone {
  const status = leaf?.status;
  const code = leaf?.reason_code || undefined;
  if (status === "running" || (workflowRunning && !status)) return "running";
  if (status === "filled") return "fresh";
  if (status === "failed" || status === "interrupted") return "fail";
  if (status === "settled") {
    if (code && FAIL_CODES.has(code)) return "fail";
    return "idle";
  }
  if (status === "gap") return code && FAIL_CODES.has(code) ? "fail" : "gap";
  if (code && FAIL_CODES.has(code)) return "fail";
  if (code && GAP_CODES.has(code)) return "gap";
  return "idle";
}

/** Prefer workflow `leaves` / `skills` from PR #4; fall back to center.components. */
export function deriveSkillRows(
  center: CenterLike | null,
  workflowRunning: boolean,
  leaves?: PanelLeaf[] | null,
): SkillRow[] {
  if (leaves?.length) {
    const byId = new Map(
      leaves
        .map((leaf) => [String(leaf.id || leaf.topic || ""), leaf] as const)
        .filter(([id]) => id),
    );
    return SKILL_ORDER.map((id) => {
      const leaf = byId.get(id);
      const code = leaf?.reason_code || undefined;
      return {
        id,
        label: SKILL_LABELS[id],
        tone: toneFromLeaf(leaf, workflowRunning),
        reason: code || undefined,
        retryable: typeof leaf?.retryable === "boolean"
          ? leaf.retryable
          : code
            ? GAP_CODES.has(code)
            : undefined,
        detail: leaf?.summary,
      };
    });
  }

  // Legacy fallback until workflow leaves land on this machine.
  const comps = center?.components || {};
  const pack = (
    id: SkillId,
    status: string | undefined,
    code: string | undefined,
    detail?: string,
  ): SkillRow => {
    let tone: SkillTone = "idle";
    if (workflowRunning && status === "running") tone = "running";
    else if (status === "fresh") tone = "fresh";
    else if (code && FAIL_CODES.has(code)) tone = "fail";
    else if (status === "failed" || status === "unavailable") tone = "fail";
    else if (status === "missing" || status === "stale" || status === "partial" || (code && GAP_CODES.has(code))) tone = "gap";
    else if (status === "running") tone = "running";
    return {
      id,
      label: SKILL_LABELS[id],
      tone,
      reason: code,
      retryable: code ? GAP_CODES.has(code) : undefined,
      detail,
    };
  };
  const profile = comps.profile;
  const crestMissing = profile?.crest?.status && profile.crest.status !== "fresh";
  return [
    pack("club.crest", profile?.status, crestMissing ? "CREST_MISSING" : undefined, profile?.note),
    pack("squad.info", comps.roster?.status, undefined),
    pack("squad.names", undefined, undefined),
    pack(
      "squad.images",
      comps.portraits?.status,
      undefined,
      comps.portraits?.total_count != null
        ? `${comps.portraits.available_count || 0} / ${comps.portraits.total_count} 张`
        : undefined,
    ),
    pack("schedule.standings", comps.schedule?.status, center?.coverage?.has_full_schedule === false ? "INCOMPLETE_SCHEDULE" : undefined),
    pack(
      "stats.charts",
      center?.chart_blocks?.status === "ready" ? "fresh" : "missing",
      center?.chart_blocks?.reason_code || "SAMPLE_INSUFFICIENT",
    ),
  ];
}

export function useTeamWorkflow(teamId: string, onChange?: () => void) {
  const endpoint = `/workflows/team/${encodeURIComponent(teamId)}`;
  const [state, setState] = useState<PanelWorkflowState | null>(null);
  const [error, setError] = useState("");
  const [pending, setPending] = useState<"start" | "stop" | null>(null);
  const mounted = useRef(true);
  const acting = useRef(false);
  const onChanged = useRef(onChange);
  onChanged.current = onChange;

  const load = useCallback(async () => {
    if (!mounted.current || acting.current) return;
    try {
      const next = await api<PanelWorkflowState>(endpoint);
      if (!mounted.current) return;
      setState(next);
      setError("");
    } catch (err) {
      if (mounted.current) setError(errorText(err));
    }
  }, [endpoint]);

  useEffect(() => {
    mounted.current = true;
    setState(null);
    setError("");
    setPending(null);
    void load();
    return () => {
      mounted.current = false;
    };
  }, [load]);

  const active = !!state && (state.enabled || state.status === "running" || state.status === "stopping");

  useEffect(() => {
    if (!active) return;
    const timer = window.setInterval(() => void load(), 4000);
    return () => clearInterval(timer);
  }, [active, load]);

  const control = async (action: "start" | "stop") => {
    if (!mounted.current || acting.current) return;
    if (action === "start" && active) return;
    if (action === "stop" && !active) return;
    acting.current = true;
    setPending(action);
    setError("");
    try {
      const next = await post<PanelWorkflowState>(`${endpoint}/${action}`);
      if (mounted.current) {
        setState(next);
        onChanged.current?.();
      }
    } catch (err) {
      if (mounted.current) setError(errorText(err));
    } finally {
      acting.current = false;
      if (mounted.current) setPending(null);
    }
  };

  return { state, error, pending, active, load, start: () => control("start"), stop: () => control("stop") };
}

function SkillDot({ tone }: { tone: SkillTone }) {
  if (tone === "running") return <LoaderCircle size={14} className="spin skill-dot skill-dot-running" aria-hidden />;
  if (tone === "fresh") return <Check size={14} className="skill-dot skill-dot-fresh" aria-hidden />;
  return <i className={`skill-dot skill-dot-${tone}`} aria-hidden />;
}

export function TeamCompletionBar({
  rows,
  open,
  onToggle,
  summary,
}: {
  rows: SkillRow[];
  open: boolean;
  onToggle: () => void;
  summary: string;
}) {
  const running = rows.some((row) => row.tone === "running");
  const gaps = rows.filter((row) => row.tone === "gap" || row.tone === "fail").length;
  return (
    <section className={`team-fill-bar${open ? " is-open" : ""}`} aria-label="球队资料补全进度">
      <button type="button" className="team-fill-summary" onClick={onToggle} aria-expanded={open}>
        <span>{summary}</span>
        <small>
          {running ? "补全中" : gaps ? `${gaps} 项待处理` : "资料齐全"}
        </small>
      </button>
      {open && (
        <ul className="team-fill-rows">
          {rows.map((row) => (
            <li key={row.id} className={`team-fill-row tone-${row.tone}`}>
              <SkillDot tone={row.tone} />
              <div className="team-fill-copy">
                <strong>{row.label}</strong>
                <span className="team-fill-id">{row.id}</span>
                {row.detail && <small>{row.detail}</small>}
              </div>
              {row.reason && (
                <span
                  className={`skill-code${row.retryable ? " is-retryable" : ""}`}
                  title={row.retryable === false ? "不可自动重试" : row.retryable ? "可重试" : undefined}
                >
                  {row.reason}
                </span>
              )}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

export function TeamHeroActions({
  teamId,
  updatedAt,
  center,
  onChanged,
  fillOpen,
  onToggleFill,
}: {
  teamId: string;
  updatedAt?: string;
  center: CenterLike | null;
  onChanged: () => void;
  fillOpen: boolean;
  onToggleFill: () => void;
}) {
  const { state, error, pending, active, start, stop } = useTeamWorkflow(teamId, onChanged);
  const running = active || pending === "start";
  const leaves = state?.leaves || state?.skills || null;
  const rows = deriveSkillRows(center, running, leaves);
  const progress = state?.progress;
  const progressLine = progress
    ? `本轮资料：已完成 ${progress.completed}，待处理 ${progress.remaining}${progress.failed > 0 ? `，未完成 ${progress.failed}` : ""}`
    : "";
  // settled is intentional omitted from the three-cell copy.
  const summary = state?.panel?.summary
    || state?.message
    || progressLine
    || (running ? "正在按多级 skill 补全球队资料" : "点更新后按六级 skill 补全资料");

  return (
    <>
      <div className="team-hero-actions">
        <button
          type="button"
          className="button primary team-fill-trigger"
          disabled={running || pending === "stop"}
          onClick={() => {
            void start();
            if (!fillOpen) onToggleFill();
          }}
        >
          {running ? <LoaderCircle size={15} className="spin" /> : <Play size={15} />}
          {running ? "补全中…" : "更新 / 补全"}
        </button>
        {active && (
          <button
            type="button"
            className="button team-fill-stop"
            disabled={pending === "stop" || state?.status === "stopping"}
            onClick={() => void stop()}
          >
            {pending === "stop" ? <LoaderCircle size={14} className="spin" /> : <Square size={14} />}
            结束
          </button>
        )}
        <small className="team-fill-updated">
          {updatedAt ? `上次更新 · ${friendlyTime(updatedAt)}` : "尚未同步资料"}
        </small>
        {error && <p className="team-fill-error" role="alert">{error}</p>}
      </div>
      {(fillOpen || running) && (
        <TeamCompletionBar
          rows={rows}
          open
          onToggle={onToggleFill}
          summary={summary}
        />
      )}
    </>
  );
}
