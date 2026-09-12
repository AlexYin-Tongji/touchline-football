import { lazy, Suspense, useCallback, useEffect, useRef, useState } from "react";
import {
  ArrowRight,
  ArrowUpRight,
  BookOpen,
  CheckCircle2,
  ChevronDown,
  CircleAlert,
  Globe2,
  Goal,
  History,
  LayoutDashboard,
  LogOut,
  MessageCircle,
  Plus,
  Send,
  Settings as SettingsIcon,
  Sparkles,
  X,
} from "lucide-react";
import { api, errorText, post, put } from "./api";
import {
  Crest,
  dateTime,
  evidenceTitle,
  contentLabel,
  Empty,
  ErrorNotice,
  EvidenceDetail,
  Modal,
  RunView,
  safeUrl,
  Spinner,
  statusNames,
  sourceLabel,
} from "./components";
import { TeamHub, chineseText } from "./teamHub";
import { ClubPicker } from "./ClubPicker";
import { ReadingPage } from "./ReadingPage";
import { ReportReader, ReportsPage } from "./ReportViews";
import { LeaguePage, useLeagues } from "./LeaguePage";
import { useAuth } from "./AuthGate";
import type {
  AppState,
  AutoReport,
  ClubEvent,
  Evidence,
  Match,
  Mode,
  Run,
  Skill,
} from "./types";

type Panel = "preferences" | "sources" | "tracking" | "profile" | null;
type AppPage = "home" | "reports" | "report" | "reading" | "leagues" | "chat";
const SettingsPage = lazy(() => import("./pages").then(module => ({ default: module.SettingsPage })));
const SourcesPage = lazy(() => import("./pages").then(module => ({ default: module.SourcesPage })));
const TrackingPage = lazy(() => import("./v2pages").then(module => ({ default: module.TrackingPage })));
const TeamProfileEditor = lazy(() => import("./TeamProfileEditor").then(module => ({ default: module.TeamProfileEditor })));
const isActive = (run?: Run | null) =>
  run?.status === "queued" || run?.status === "running";
const navigation = [
  { id: "home" as const, label: "球队动态", icon: LayoutDashboard },
  { id: "reports" as const, label: "跟队报告", icon: Sparkles },
  { id: "reading" as const, label: "资料阅读", icon: BookOpen },
  { id: "leagues" as const, label: "五大联赛", icon: Globe2 },
  { id: "chat" as const, label: "询问助手", icon: MessageCircle },
];

export default function App() {
  const auth = useAuth();
  const [data, setData] = useState<AppState | null>(null);
  const [mode, setMode] = useState<Mode | null>(null);
  const [teamId, setTeamId] = useState(() => {
    try {
      return localStorage.getItem("touchline-team") || "";
    } catch {
      return "";
    }
  });
  const [page, setPage] = useState<AppPage>("home");
  const [report, setReport] = useState<AutoReport | null>(null);
  const [panel, setPanel] = useState<Panel>(null);
  const [event, setEvent] = useState<ClubEvent | null>(null);
  const [document, setDocument] = useState<Evidence | null>(null);
  const [documentLoading, setDocumentLoading] = useState(false);
  const [documentError, setDocumentError] = useState("");
  const [pendingEvidence, setPendingEvidence] = useState<Evidence | null>(null);
  const [pendingReportEvidence, setPendingReportEvidence] = useState<string[]>([]);
  const [selectedSkill, setSelectedSkill] = useState<Skill>("auto");
  const [question, setQuestion] = useState("");
  const [allowNetwork, setAllowNetwork] = useState(true);
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null);
  const [runDetail, setRunDetail] = useState<Run | null>(null);
  const [viewedRuns, setViewedRuns] = useState<Record<string, Run>>({});
  const [conversationId, setConversationId] = useState<string | null>(null);
  const [pendingMatch, setPendingMatch] = useState<Match | null>(null);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [busy, setBusy] = useState<Record<string, boolean>>({});
  const [loadError, setLoadError] = useState("");
  const [runError, setRunError] = useState("");
  const [toast, setToast] = useState<{
    message: string;
    error: boolean;
  } | null>(null);
  const chatBodyRef = useRef<HTMLDivElement>(null);
  const spoilerPreference = useRef<boolean | undefined>(undefined);
  const scope = useRef({ mode, teamId });
  scope.current = { mode, teamId };
  const requestSerial = useRef(0);
  const locks = useRef(new Set<string>());
  const questionRef = useRef<HTMLTextAreaElement>(null);
  const notice = useCallback(
    (message: string, error = false) => setToast({ message, error }),
    [],
  );
  const closePanel = useCallback(() => setPanel(null), []);
  const closeEvent = useCallback(() => setEvent(null), []);
  const closeDocument = useCallback(() => setDocument(null), []);
  const leagueState = useLeagues(data?.settings.spoiler_free);

  useEffect(() => {
    if (!document) return;
    let current = true;
    setDocumentLoading(true);
    setDocumentError("");
    api<Evidence>(`/evidence/${encodeURIComponent(document.id)}`).then(item => {
      if (current) setDocument(item);
    }).catch(error => { if (current) setDocumentError(errorText(error)); }).finally(() => { if (current) setDocumentLoading(false); });
    return () => { current = false; };
  }, [document?.id, data?.settings.spoiler_free]);

  const reload = useCallback(async () => {
    const serial = ++requestSerial.current;
    const requestedMode = scope.current.mode;
    const requestedTeam = scope.current.teamId;
    try {
      const result = await api<AppState>(
        `/state?${new URLSearchParams({ ...(requestedMode ? { mode: requestedMode } : {}), ...(requestedTeam ? { team_id: requestedTeam } : {}) })}`,
      );
      if (
        serial !== requestSerial.current ||
        requestedMode !== scope.current.mode || requestedTeam !== scope.current.teamId
      )
        return;
      const next = {
        ...result,
        settings: {
          ...result.settings,
          mode: requestedMode || result.settings.mode,
        },
        teams: result.teams || [],
        events: result.events || [],
        evidence: result.evidence || [],
        matches: result.matches || [],
        runs: result.runs || [],
        sources: result.sources || [],
      };
      if (
        spoilerPreference.current !== undefined &&
        spoilerPreference.current !== next.settings.spoiler_free
      ) {
        setRunDetail(null);
        setViewedRuns({});
        setEvent(null);
        setDocument(null);
        setPendingEvidence(null);
        setPendingReportEvidence([]);
        setReport(null);
        setPage(previous => previous === "report" ? "reports" : previous);
      }
      spoilerPreference.current = next.settings.spoiler_free;
      setData(next);
      setLoadError("");
      if (!requestedMode) {
        scope.current.mode = next.settings.mode;
        setMode(next.settings.mode);
      }
      setTeamId((previous) =>
        next.teams.some((t) => t.id === previous)
          ? previous
          : next.settings.followed_teams?.find((id) =>
              next.teams.some((t) => t.id === id),
            ) ||
            next.teams[0]?.id ||
            "",
      );
    } catch (error) {
      if (serial === requestSerial.current) setLoadError(errorText(error));
      throw error;
    }
  }, []);
  useEffect(() => {
    reload().catch(() => undefined);
  }, [reload, teamId]);
  useEffect(() => {
    if (!toast) return;
    const timer = window.setTimeout(() => setToast(null), 5500);
    return () => window.clearTimeout(timer);
  }, [toast]);
  const runs = [
    ...Object.values(viewedRuns),
    ...(data?.runs || []).filter((r) => !viewedRuns[r.id]),
  ]
    .filter((r) => r.mode === mode && r.team_id === teamId && !r.internal_report && String(r.skill) !== "news_report")
    .sort((a, b) => (b.created_at || "").localeCompare(a.created_at || ""));
  const selectedRun =
    runDetail?.id === selectedRunId &&
    runDetail.mode === mode &&
    runDetail.team_id === teamId
      ? runDetail
      : runs.find((r) => r.id === selectedRunId) || null;
  const running = runs.find(isActive);
  const active = isActive(selectedRun) || !!running;
  const reportsRunning = data?.reports?.some(item => item.status === "generating" || item.status === "queued" && data.settings.reports?.auto_generate !== false && (item.budget?.remaining_today ?? 1) > 0);
  useEffect(() => {
    const tick = () => {
      if (window.document.visibilityState === "visible")
        reload().catch(() => undefined);
    };
    const timer = window.setInterval(
      tick,
      data?.automation_status?.running || reportsRunning ? 2500 : 30000,
    );
    window.addEventListener("focus", tick);
    return () => {
      window.clearInterval(timer);
      window.removeEventListener("focus", tick);
    };
  }, [reload, data?.automation_status?.running, reportsRunning]);
  const activeIds = runs
    .filter(isActive)
    .map((r) => r.id)
    .sort()
    .join(",");
  useEffect(() => {
    const ids = [
      ...new Set(
        [selectedRunId, ...activeIds.split(",")].filter(
          (id): id is string => !!id,
        ),
      ),
    ];
    if (!ids.length) return;
    let cancelled = false;
    let timer: number | undefined;
    const requestScope = { mode, teamId };
    const previouslyActive = new Set(activeIds.split(","));
    const read = async () => {
      const results = await Promise.allSettled(
        ids.map((id) => api<Run>(`/runs/${id}`)),
      );
      if (
        cancelled ||
        requestScope.mode !== scope.current.mode ||
        requestScope.teamId !== scope.current.teamId
      )
        return;
      const updated = results
        .filter(
          (r): r is PromiseFulfilledResult<Run> => r.status === "fulfilled",
        )
        .map((r) => r.value);
      const failed = results.find(
        (r): r is PromiseRejectedResult => r.status === "rejected",
      );
      setRunError(failed ? errorText(failed.reason) : "");
      const selected = updated.find((r) => r.id === selectedRunId);
      if (selected) {
        setRunDetail(selected);
        setViewedRuns((previous) => ({ ...previous, [selected.id]: selected }));
      }
      if (updated.length)
        setData((previous) =>
          previous
            ? {
                ...previous,
                runs: [
                  ...updated,
                  ...previous.runs.filter(
                    (r) => !updated.some((next) => next.id === r.id),
                  ),
                ],
              }
            : previous,
        );
      const completed = updated.some(
        (r) => previouslyActive.has(r.id) && !isActive(r),
      );
      for (const run of updated)
        if (!isActive(run)) previouslyActive.delete(run.id);
      if (completed) await reload().catch(() => undefined);
      if (!cancelled && (updated.some(isActive) || failed))
        timer = window.setTimeout(read, failed ? 4000 : 1200);
    };
    read();
    return () => {
      cancelled = true;
      if (timer) window.clearTimeout(timer);
    };
  }, [
    selectedRunId,
    activeIds,
    mode,
    teamId,
    data?.settings.spoiler_free,
    reload,
  ]);
  useEffect(() => {
    const body = chatBodyRef.current;
    if (body) body.scrollTop = body.scrollHeight;
  }, [selectedRunId, selectedRun?.status]);
  async function act(key: string, work: () => Promise<void>) {
    if (locks.current.has(key)) return;
    locks.current.add(key);
    setBusy((prev) => ({ ...prev, [key]: true }));
    try {
      await work();
    } catch (error) {
      notice(errorText(error), true);
    } finally {
      locks.current.delete(key);
      setBusy((prev) => ({ ...prev, [key]: false }));
    }
  }
  function resetConversation() {
    setSelectedRunId(null);
    setRunDetail(null);
    setConversationId(null);
    setQuestion("");
    setPendingMatch(null);
    setPendingEvidence(null);
    setPendingReportEvidence([]);
    setSelectedSkill("auto");
    setRunError("");
  }
  function switchTeam(id: string) {
    try {
      localStorage.setItem("touchline-team", id);
    } catch {
      /* storage is optional */
    }
    scope.current.teamId = id;
    setTeamId(id);
    setViewedRuns({});
    resetConversation();
    setEvent(null);
    setDocument(null);
    setReport(null);
    setPage(previous => previous === "report" ? "reports" : previous);
    setHistoryOpen(false);
  }
  async function switchMode(next: Mode) {
    if (next === mode) return;
    await act("mode", async () => {
      await put("/settings", { mode: next });
      ++requestSerial.current;
      scope.current.mode = next;
      setMode(next);
      setData(null);
      setViewedRuns({});
      resetConversation();
      setEvent(null);
      setDocument(null);
      setReport(null);
      setPage(previous => previous === "report" ? "reports" : previous);
      await reload();
    });
  }
  function navigate(next: AppPage) {
    setPage(next);
    if (window.location.hash) window.history.replaceState(null, "", window.location.pathname + window.location.search);
    window.setTimeout(() => window.document.getElementById("main-content")?.focus({ preventScroll: true }), 0);
    window.scrollTo({ top: 0, behavior: "smooth" });
  }
  function ask(text: string, match?: Match) {
    setQuestion(text);
    setPendingMatch(match || null);
    setPendingEvidence(null);
    setPendingReportEvidence([]);
    setSelectedSkill("auto");
    setEvent(null);
    setDocument(null);
    setPanel(null);
    navigate("chat");
    window.setTimeout(() => questionRef.current?.focus(), 100);
  }
  function askFromArticle(text: string, item: Evidence) {
    resetConversation();
    ask(text);
    setPendingEvidence(item);
  }
  function openReport(item: AutoReport) {
    setReport(item);
    setEvent(null);
    navigate("report");
  }
  function askFromReport(text: string, item: AutoReport) {
    resetConversation();
    ask(text);
    setPendingReportEvidence(item.report?.sources.map(source => source.id) || []);
  }
  async function retryReport(item: AutoReport) {
    await act(`report-${item.id}`, async () => {
      const updated = await post<AutoReport>(`/reports/${encodeURIComponent(item.id)}/retry`);
      if (updated?.id === item.id) setReport(previous => previous?.id === item.id ? updated : previous);
      await reload();
      notice("报告已加入整理队列，完成后会自动显示。");
    });
  }
  function openRun(run: Run) {
    setSelectedRunId(run.id);
    setRunDetail(run);
    setConversationId(run.conversation_id);
    setQuestion("");
    setPendingEvidence(null);
    setPendingReportEvidence([]);
    setSelectedSkill(run.skill);
    setPendingMatch(data?.matches.find((m) => m.id === run.match_id) || null);
    setPanel(null);
    setHistoryOpen(false);
    navigate("chat");
  }
  async function startRun(
    text: string,
    skill: Skill = "auto",
    match?: Match | null,
  ) {
    if (
      !text.trim() ||
      !teamId ||
      !mode ||
      active ||
      locks.current.has("start")
    )
      return;
    if (mode === "live" && !data?.settings.llm.model) {
      notice("连接模型后，就可以开始提问。");
      setPanel("preferences");
      return;
    }
    const requested = { mode, teamId };
    const contextualMatch =
      match === null
        ? null
        : match || data?.matches.find((m) => m.id === selectedRun?.match_id);
    const effectiveMatch =
      contextualMatch?.kickoff &&
      Number.isFinite(Date.parse(contextualMatch.kickoff))
        ? contextualMatch
        : undefined;
    const effectiveSkill = effectiveMatch
      ? effectiveMatch.status === "finished"
        ? "match_review"
        : "match_preview"
      : ["match_preview", "match_review"].includes(skill)
        ? "auto"
        : skill;
    await act("start", async () => {
      const run = await post<Run>("/runs", {
        mode,
        team_id: teamId,
        question: text.trim(),
        skill: effectiveSkill,
        allow_network: allowNetwork,
        conversation_id: conversationId || undefined,
        match_id: effectiveMatch?.id,
        evidence_ids: pendingEvidence ? [pendingEvidence.id] : pendingReportEvidence.length ? pendingReportEvidence : undefined,
      });
      if (
        requested.mode !== scope.current.mode ||
        requested.teamId !== scope.current.teamId
      )
        return;
      setData((previous) =>
        previous
          ? {
              ...previous,
              runs: [run, ...previous.runs.filter((r) => r.id !== run.id)],
            }
          : previous,
      );
      setSelectedRunId(run.id);
      setRunDetail(run);
      setConversationId(run.conversation_id);
      setQuestion("");
      setPendingMatch(effectiveMatch || null);
      setRunError("");
      navigate("chat");
    });
  }
  async function cancelRun(target?: Run) {
    const run = target || selectedRun || running;
    if (!run) return;
    await act("cancel", async () => {
      const next = await post<Run>(`/runs/${run.id}/cancel`);
      if (
        next.mode === scope.current.mode &&
        next.team_id === scope.current.teamId
      ) {
        if (next.id === selectedRunId) setRunDetail(next);
        setViewedRuns((prev) =>
          prev[next.id] ? { ...prev, [next.id]: next } : prev,
        );
        await reload();
      }
    });
  }
  async function updateTeam() {
    if (!mode) return;
    if (mode === "demo") {
      notice("当前展示的是虚构示例。切换“真实跟队”后可更新消息。");
      return;
    }
    await act("update", async () => {
      await post("/automation/run", { team_id: teamId });
      await reload();
      notice("正在更新球队消息和赛程。");
    });
  }
  async function watch(item: ClubEvent) {
    await act(`watch-${item.id}`, async () => {
      await post(`/events/${item.id}/watch`, { watching: !item.watching });
      await reload();
    });
  }
  if (!data || !mode)
    return (
      <div className="boot-screen">
        <span className="v4-brand">
          <Goal size={28} />
          看台
        </span>
        {loadError ? (
          <>
            <ErrorNotice>{loadError}</ErrorNotice>
            <button
              className="button"
              onClick={() => reload().catch(() => undefined)}
            >
              重新连接
            </button>
          </>
        ) : (
          <Spinner text="正在准备你的主场" />
        )}
      </div>
    );
  const team = data.teams.find((t) => t.id === teamId);
  const evidence = data.evidence.filter((e) => e.team_id === teamId);
  const reports = (data.reports || []).filter(item => item.team_id === teamId && item.mode === mode);
  const modelConfigured = data.setup?.model_configured ?? !!data.settings.llm.model;
  const eventSources = event?.evidence_ids.map(id => evidence.find(item => item.id === id)).filter((item): item is Evidence => !!item) || [];
  const conversationRuns = selectedRun
    ? [selectedRun, ...runs.filter((r) => r.id !== selectedRun.id)].sort(
        (a, b) => (b.created_at || "").localeCompare(a.created_at || ""),
      )
    : runs;
  const conversation = selectedRun
    ? conversationRuns
        .filter(
          (r) =>
            r.conversation_id === selectedRun.conversation_id &&
            (r.created_at || "") <= (selectedRun.created_at || "\uffff"),
        )
        .reverse()
    : [];
  const renderRun = (run: Run) => (
    <RunView
      run={run.id === selectedRun?.id ? selectedRun : run}
      evidence={evidence}
      openEvidence={setDocument}
      onCancel={() => cancelRun(run)}
      cancelling={!!busy.cancel}
      onRetry={() =>
        startRun(
          run.question,
          run.skill,
          data.matches.find((m) => m.id === run.match_id),
        )
      }
      onSettings={() => setPanel("preferences")}
      onResume={() => {
        if (active || locks.current.has("start")) return;
        return act("start", async () => {
          const next = await post<Run>(`/runs/${run.id}/resume`);
          if (
            next.mode !== scope.current.mode ||
            next.team_id !== scope.current.teamId
          )
            return;
          setData((prev) =>
            prev ? { ...prev, runs: [next, ...prev.runs] } : prev,
          );
          openRun(next);
        });
      }}
    />
  );
  return (
    <div className="touchline-app">
      <a className="skip-link" href="#main-content">跳转到主要内容</a>
      <header className="v4-header">
        <div className="v4-header-inner">
          <button
            className="v4-brand"
            onClick={() => navigate("home")}
            aria-label="看台首页"
          >
            <span className="v4-brand-icon">
              <Goal size={25} strokeWidth={1.7} />
            </span>
            看台<span className="brand-tagline">你的足球主场</span>
          </button>
          <nav className="v4-nav" aria-label="主导航">
            {navigation.map((item) => (
              <button
                key={item.id}
                className={page === item.id || page === "report" && item.id === "reports" ? "active" : ""}
                aria-current={page === item.id || page === "report" && item.id === "reports" ? "page" : undefined}
                onClick={() => navigate(item.id)}
              >
                <item.icon size={17} />
                {item.label}
              </button>
            ))}
          </nav>
          <div className="header-actions"><button
            className="settings-trigger"
            onClick={() => setPanel("preferences")}
            aria-label="打开设置"
          >
            <SettingsIcon size={20} />
            <span>设置</span>
          </button>
          {auth.status?.required && <button className="icon-button logout-trigger" aria-label="退出登录" title="退出登录" onClick={() => act("logout", auth.logout)} disabled={!!busy.logout}><LogOut size={18} /></button>}</div>
        </div>
      </header>
      <div className="v4-clubbar">
        <div className="clubbar-inner">
          <ClubPicker teams={data.teams} teamId={teamId} followed={data.settings.followed_teams || []} onSelect={switchTeam} leagues={leagueState.leagues} seasons={leagueState.available_seasons} season={leagueState.season} onSeason={leagueState.setSeason} />
          <label className={`space-switch ${mode}`}>
            <span className="space-dot" />
            <select
              aria-label="资料模式"
              value={mode}
              disabled={!!busy.mode}
              onChange={(e) => switchMode(e.target.value as Mode)}
            >
              <option value="live">真实跟队</option>
              <option value="demo">体验示例</option>
            </select>
            <ChevronDown size={13} />
          </label>
        </div>
      </div>
      <main
        className={`v4-main ${page === "chat" ? "is-chat" : ""}`}
        id="main-content"
        tabIndex={-1}
      >
        {mode === "demo" && page !== "leagues" && (
          <div className="demo-banner">
            <Sparkles size={15} />
            <span>体验模式 · 动态、对手和赛果为虚构示例</span>
            <button disabled={busy.mode} onClick={() => switchMode("live")}>
              开始真实跟队
              <ArrowRight size={14} />
            </button>
          </div>
        )}
        <ErrorNotice>{loadError}</ErrorNotice>
        {data.automation_status?.last_error && (
          <div className="update-notice" role="status">
            部分资料暂未更新，可稍后重试。
            <button onClick={() => setPanel("sources")}>查看来源</button>
          </div>
        )}
        {team ? (
          page === "home" ? (
            <TeamHub
              key={`${mode}-${teamId}`}
              data={data}
              team={team}
              busy={!!busy.update || !!data.automation_status?.running}
              onUpdate={updateTeam}
              onAsk={ask}
              onWatch={watch}
              onEvent={(item) => {
                const existingReport = reports.find(report => report.event_id === item.id);
                if (existingReport) openReport(existingReport);
                else setEvent(item);
                if (!item.read)
                  act(`read-${item.id}`, async () => {
                    await post(`/events/${item.id}/read`, { read: true });
                    await reload();
                  });
              }}
              onSettings={() => setPanel("preferences")}
              onProfile={() => setPanel("profile")}
              onRun={openRun}
              onEvidence={setDocument}
              onReading={() => navigate("reading")}
              reports={reports}
              onReport={openReport}
              onReports={() => navigate("reports")}
              onRetryReport={retryReport}
              league={leagueState.leagues.find(league => league.teams.some(member => member.id === team.id))}
            />
          ) : page === "leagues" ? (
            <LeaguePage state={leagueState} onTeam={id => { switchTeam(id); navigate("home"); }} onSettings={() => setPanel("preferences")} />
          ) : page === "reports" || page === "report" && !report ? (
            <ReportsPage team={team} reports={reports} onRead={openReport} onSettings={() => setPanel("preferences")} onRetry={retryReport} onSources={() => setPanel("sources")} configured={modelConfigured} automatic={data.settings.reports?.auto_generate ?? true} />
          ) : page === "report" && report ? (
            <ReportReader key={report.id} item={reports.find(item => item.id === report.id) || report} team={team} evidence={evidence} spoiler={data.settings.spoiler_free} onBack={() => navigate("reports")} onEvidence={setDocument} onAsk={askFromReport} onSettings={() => setPanel("preferences")} onRetry={retryReport} />
          ) : page === "reading" ? (
            <ReadingPage key={`${mode}-${teamId}`} evidence={evidence} team={team} onRead={setDocument} onSources={() => setPanel("sources")} reports={reports} onReport={openReport} />
          ) : (
            <div className="assistant-view">
              <div className="assistant-heading">
                <div>
                  <span className="overline">看台助手</span>
                  <h1>关于{team.name}，尽管问。</h1>
                </div>
                <div className="assistant-heading-actions">
                  <button
                    className="button history-toggle"
                    onClick={() => setHistoryOpen(!historyOpen)}
                    aria-expanded={historyOpen}
                  >
                    <History size={16} />
                    历史对话
                  </button>
                  <button
                    className="button"
                    disabled={active}
                    onClick={resetConversation}
                  >
                    <Plus size={17} />
                    新对话
                  </button>
                </div>
              </div>
              <div
                className={`assistant-layout ${historyOpen ? "show-history" : ""}`}
              >
                <aside className="v4-history">
                  <div className="hub-section-heading">
                    <h2>最近聊过</h2>
                    <History size={17} />
                  </div>
                  {runs.length ? (
                    runs
                      .filter(
                        (r, index, all) =>
                          all.findIndex(
                            (x) => x.conversation_id === r.conversation_id,
                          ) === index,
                      )
                      .slice(0, 30)
                      .map((run) => (
                        <button
                          key={run.id}
                          className={
                            selectedRun?.conversation_id === run.conversation_id
                              ? "active"
                              : ""
                          }
                          disabled={
                            active &&
                            run.id !== running?.id &&
                            run.id !== selectedRunId
                          }
                          onClick={() => openRun(run)}
                        >
                          <MessageCircle size={16} />
                          <div>
                            <strong>
                              {chineseText(run.question, "球队问答")}
                            </strong>
                            <span>
                              {dateTime(run.created_at)} ·{" "}
                              {statusNames[run.status]}
                            </span>
                          </div>
                        </button>
                      ))
                  ) : (
                    <p className="inline-empty">你的对话会保存在这里。</p>
                  )}
                  <div className="history-save-note">
                    <CheckCircle2 size={14} />
                    对话与查到的资料，自动保存在{data.runtime?.server_mode ? "服务器" : "本机"}
                  </div>
                </aside>
                <section className="assistant-chat">
                  <div
                    ref={chatBodyRef}
                    className={`chat-body ${selectedRun ? "has-conversation" : ""}`}
                  >
                    {selectedRun ? (
                      <>
                        <ErrorNotice>{runError}</ErrorNotice>
                        {conversation.map((run) => (
                          <div className="conversation-turn" key={run.id}>
                            <div className="user-question">
                              <span>你</span>
                              <p>{chineseText(run.question, "球队问答")}</p>
                            </div>
                            <div className="assistant-answer">
                              <span className="answer-avatar">
                                <Sparkles size={18} />
                              </span>
                              <div>{renderRun(run)}</div>
                            </div>
                          </div>
                        ))}
                      </>
                    ) : (
                      <div className="assistant-welcome">
                        <div className="assistant-orbit">
                          <span />
                          <div>
                            <Sparkles size={34} strokeWidth={1.4} />
                          </div>
                        </div>
                        <span className="welcome-kicker">
                          和懂球的助手，聊聊你的主队
                        </span>
                        <h2>
                          球场上的事，
                          <br />
                          一起聊明白。
                        </h2>
                        <p>近况、伤停、转会，或下一场的期待。</p>
                        <div className="starter-prompts">
                          {[
                            "最近有哪些值得关注的变化？",
                            "下一场比赛，应该关注什么？",
                            "伤停和转会，现在有什么进展？",
                          ].map((text, i) => (
                            <button key={text} onClick={() => ask(text)}>
                              <span>
                                {["球队近况", "比赛前瞻", "阵容动态"][i]}
                              </span>
                              <strong>{text}</strong>
                              <ArrowUpRight size={17} />
                            </button>
                          ))}
                        </div>
                        {mode === "live" && !data.settings.llm.model && (
                          <button
                            className="connect-model-link"
                            onClick={() => setPanel("preferences")}
                          >
                            连接模型，开始聊天
                            <ArrowRight size={15} />
                          </button>
                        )}
                      </div>
                    )}
                  </div>
                  {running && running.id !== selectedRunId && (
                    <div className="existing-run">
                      <Spinner text="还有一个问题正在整理" />
                      <button
                        className="text-button"
                        onClick={() => openRun(running)}
                      >
                        查看回答
                      </button>
                    </div>
                  )}
                  <form
                    className="v4-composer"
                    onSubmit={(e) => {
                      e.preventDefault();
                      startRun(question, selectedSkill, pendingMatch);
                    }}
                  >
                    {pendingEvidence && <div className="question-context article-context"><BookOpen size={14} /><span>正在讨论：{evidenceTitle(pendingEvidence)}</span><button type="button" aria-label="移除报道上下文" onClick={() => setPendingEvidence(null)}><X size={13} /></button></div>}
                    {pendingReportEvidence.length > 0 && <div className="question-context article-context"><BookOpen size={14} /><span>已带入本篇报告的 {pendingReportEvidence.length} 份引用资料</span><button type="button" aria-label="移除报告引用资料" onClick={() => setPendingReportEvidence([])}><X size={13} /></button></div>}
                    {pendingMatch && (
                      <div className="question-context">
                        {pendingMatch.status === "finished"
                          ? "比赛复盘"
                          : "比赛前瞻"}
                        <button
                          type="button"
                          aria-label="移除比赛上下文"
                          onClick={() => setPendingMatch(null)}
                        >
                          <X size={13} />
                        </button>
                      </div>
                    )}
                    <label className="sr-only" htmlFor="question">
                      向看台助手提问
                    </label>
                    <textarea
                      id="question"
                      ref={questionRef}
                      value={question}
                      onChange={(e) => setQuestion(e.target.value)}
                      maxLength={4000}
                      rows={2}
                      placeholder={`想了解${team.name}的什么？`}
                      onKeyDown={(e) => {
                        if (
                          e.key === "Enter" &&
                          !e.shiftKey &&
                          !e.nativeEvent.isComposing
                        ) {
                          e.preventDefault();
                          startRun(question, selectedSkill, pendingMatch);
                        }
                      }}
                    />
                    <div className="v4-composer-bottom">
                      <label className="skill-select"><span className="sr-only">助手工作方式</span><select aria-label="助手工作方式" value={selectedSkill} onChange={e => setSelectedSkill(e.target.value as Skill)}><option value="auto">智能选择</option><option value="team_brief">球队简报</option><option value="transfer_check">转会核验</option><option value="tactics_explainer">战术解读</option></select></label>
                      <label
                        className={`network-toggle ${allowNetwork ? "on" : ""}`}
                      >
                        <input
                          type="checkbox"
                          checked={allowNetwork}
                          onChange={(e) => setAllowNetwork(e.target.checked)}
                        />
                        <Globe2 size={15} />
                        {allowNetwork ? "联网查找" : "仅用已存资料"}
                      </label>
                      <button
                        className="send-question"
                        disabled={!question.trim() || !!busy.start || active}
                        aria-label="发送问题"
                      >
                        {busy.start ? (
                          <Spinner text="提交中" />
                        ) : (
                          <>
                            <span>发送</span>
                            <Send size={16} />
                          </>
                        )}
                      </button>
                    </div>
                  </form>
                </section>
              </div>
            </div>
          )
        ) : (
          <Empty
            title="添加一支球队，开启你的看台"
            action={
              <button
                className="button dark-button"
                onClick={() => setPanel("preferences")}
              >
                添加球队
              </button>
            }
          />
        )}
        <footer className="v4-footer">
          <span>
            <Goal size={15} />
            看台
          </span>
          <span>热爱有主场。</span>
          {data.runtime?.server_mode && <span>资料保存在服务器</span>}
        </footer>
      </main>
      {toast && (
        <div
          className={`toast ${toast.error ? "error" : ""}`}
          role={toast.error ? "alert" : "status"}
        >
          {toast.error ? <CircleAlert size={19} /> : <CheckCircle2 size={19} />}
          <span>{toast.message}</span>
          <button aria-label="关闭提示" onClick={() => setToast(null)}>
            <X size={16} />
          </button>
        </div>
      )}
      {panel && (
        <Modal title="看台设置" wide onClose={closePanel}>
          <div className="settings-tabs">
            {[
              ["preferences", "使用偏好"],
              ["profile", "球队资料"],
              ["sources", "资料来源"],
              ["tracking", "关注主题"],
            ].map(([id, label]) => (
              <button
                className={panel === id ? "active" : ""}
                key={id}
                onClick={() => setPanel(id as Panel)}
              >
                {label}
              </button>
            ))}
          </div>
          <div className="settings-content">
            <Suspense fallback={<Spinner text="正在打开设置" />}>
            {panel === "preferences" ? (
              <SettingsPage data={data} reload={reload} notice={notice} />
            ) : panel === "profile" ? (
              team && (
                <TeamProfileEditor
                  team={team}
                  onSaved={reload}
                  notice={notice}
                />
              )
            ) : panel === "sources" ? (
              <SourcesPage
                key={`${mode}-${teamId}`}
                data={data}
                teamId={teamId}
                mode={mode}
                reload={reload}
                notice={notice}
                switchLive={() => switchMode("live")}
                openEvidence={setDocument}
              />
            ) : (
              <TrackingPage
                key={`${mode}-${teamId}`}
                data={data}
                teamId={teamId}
                mode={mode}
                seed={null}
                clearSeed={() => undefined}
                reload={reload}
                notice={notice}
                switchLive={() => switchMode("live")}
                openRun={openRun}
              />
            )}
            </Suspense>
          </div>
        </Modal>
      )}
      {event && (
        <Modal title="来源动态" onClose={closeEvent} wide>
          <div className="event-reading">
            <span className="quiet-label">{dateTime(event.updated_at)}</span>
            <h2>
              {event.summary_origin === "pending" && eventSources[0] ? evidenceTitle(eventSources[0]) : event.display_title || event.title || "球队最新消息"}
            </h2>
            <p>
              {event.summary_origin === "pending" ? eventSources[0]?.text?.slice(0, 800) || "原文资料正在提取，可从下方打开来源。" : event.display_summary || event.summary || event.change || eventSources[0]?.text?.slice(0, 800)}
            </p>
            {event.uncertainties
              ?.filter((x) => /[\u3400-\u9fff]/.test(x) && !/来源内容不等于|需要对照原文|独立证据/.test(x))
              .map((text, index) => (
                <p className="event-uncertainty" key={index}>
                  {text}
                </p>
              ))}
            <h3 className="event-source-heading"><BookOpen size={17} />阅读相关报道</h3>
            <div className="event-source-list">
              {event.evidence_ids
                .map((id) => evidence.find((e) => e.id === id))
                .filter((e): e is Evidence => !!e)
                .map((item) => <button key={item.id} onClick={() => { setEvent(null); setDocument(item); }}><span><strong>{evidenceTitle(item)}</strong><small>{sourceLabel(item)} · {contentLabel(item)}</small></span><ArrowRight size={17} /></button>)}
            </div>
            <button
              className="button dark-button"
              onClick={() =>
                ask(
                  `解读“${event.display_title || event.title}”，对${team?.name}有什么影响？`,
                )
              }
            >
              <Sparkles size={16} />
              围绕资料提问
            </button>
          </div>
        </Modal>
      )}
      {document && (
        <Modal title="报道阅读" onClose={closeDocument} wide>
          {documentLoading && <div className="reader-loading"><Spinner text="正在获取最新正文" /></div>}
          <ErrorNotice>{documentError ? `正文更新未完成，下面显示已收录的内容。${documentError}` : ""}</ErrorNotice>
          {reports.filter(item => item.status === "completed" && item.report?.sources.some(source => source.id === document.id)).slice(0, 1).map(item => <button key={item.id} className="reader-report-link" onClick={() => { setDocument(null); openReport(item); }}><Sparkles size={16} /><span>这篇来源已整理成中文报告<strong>{item.report!.title}</strong></span><ArrowRight size={17} /></button>)}
          <EvidenceDetail key={document.id} item={document} onAsk={askFromArticle} />
        </Modal>
      )}
    </div>
  );
}
