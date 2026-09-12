import { useCallback, useEffect, useRef, useState } from "react";
import {
  ArrowRight,
  ArrowUpRight,
  CheckCircle2,
  ChevronDown,
  CircleAlert,
  Globe2,
  Goal,
  History,
  LayoutDashboard,
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
import { SettingsPage, SourcesPage } from "./pages";
import { TrackingPage } from "./v2pages";
import { TeamHub, chineseText } from "./teamHub";
import { TeamProfileEditor } from "./TeamProfileEditor";
import type {
  AppState,
  ClubEvent,
  Evidence,
  Match,
  Mode,
  Run,
  Skill,
} from "./types";

type Panel = "preferences" | "sources" | "tracking" | "profile" | null;
const isActive = (run?: Run | null) =>
  run?.status === "queued" || run?.status === "running";
const navigation = [
  { id: "home" as const, label: "球队动态", icon: LayoutDashboard },
  { id: "chat" as const, label: "询问助手", icon: MessageCircle },
];

export default function App() {
  const [data, setData] = useState<AppState | null>(null);
  const [mode, setMode] = useState<Mode | null>(null);
  const [teamId, setTeamId] = useState(() => {
    try {
      return localStorage.getItem("touchline-team") || "";
    } catch {
      return "";
    }
  });
  const [page, setPage] = useState<"home" | "chat">("home");
  const [panel, setPanel] = useState<Panel>(null);
  const [event, setEvent] = useState<ClubEvent | null>(null);
  const [document, setDocument] = useState<Evidence | null>(null);
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

  const reload = useCallback(async () => {
    const serial = ++requestSerial.current;
    const requestedMode = scope.current.mode;
    try {
      const result = await api<AppState>(
        requestedMode ? `/state?mode=${requestedMode}` : "/state",
      );
      if (
        serial !== requestSerial.current ||
        requestedMode !== scope.current.mode
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
  }, [reload]);
  useEffect(() => {
    if (!toast) return;
    const timer = window.setTimeout(() => setToast(null), 5500);
    return () => window.clearTimeout(timer);
  }, [toast]);
  const runs = [
    ...Object.values(viewedRuns),
    ...(data?.runs || []).filter((r) => !viewedRuns[r.id]),
  ]
    .filter((r) => r.mode === mode && r.team_id === teamId)
    .sort((a, b) => (b.created_at || "").localeCompare(a.created_at || ""));
  const selectedRun =
    runDetail?.id === selectedRunId &&
    runDetail.mode === mode &&
    runDetail.team_id === teamId
      ? runDetail
      : runs.find((r) => r.id === selectedRunId) || null;
  const running = runs.find(isActive);
  const active = isActive(selectedRun) || !!running;
  useEffect(() => {
    const tick = () => {
      if (window.document.visibilityState === "visible")
        reload().catch(() => undefined);
    };
    const timer = window.setInterval(
      tick,
      data?.automation_status?.running ? 2500 : 30000,
    );
    window.addEventListener("focus", tick);
    return () => {
      window.clearInterval(timer);
      window.removeEventListener("focus", tick);
    };
  }, [reload, data?.automation_status?.running]);
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
      await reload();
    });
  }
  function navigate(next: "home" | "chat") {
    setPage(next);
    window.scrollTo({ top: 0, behavior: "smooth" });
  }
  function ask(text: string, match?: Match) {
    setQuestion(text);
    setPendingMatch(match || null);
    setEvent(null);
    setPanel(null);
    navigate("chat");
    window.setTimeout(() => questionRef.current?.focus(), 100);
  }
  function openRun(run: Run) {
    setSelectedRunId(run.id);
    setRunDetail(run);
    setConversationId(run.conversation_id);
    setQuestion("");
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
                className={page === item.id ? "active" : ""}
                aria-current={page === item.id ? "page" : undefined}
                onClick={() => navigate(item.id)}
              >
                <item.icon size={17} />
                {item.label}
              </button>
            ))}
          </nav>
          <button
            className="settings-trigger"
            onClick={() => setPanel("preferences")}
            aria-label="打开设置"
          >
            <SettingsIcon size={20} />
            <span>设置</span>
          </button>
        </div>
      </header>
      <div className="v4-clubbar">
        <div className="clubbar-inner">
          <div className="club-tabs" aria-label="选择球队">
            {data.teams.map((t) => (
              <button
                key={t.id}
                className={teamId === t.id ? "active" : ""}
                aria-pressed={teamId === t.id}
                onClick={() => switchTeam(t.id)}
              >
                <Crest team={t} small />
                <span>{t.name}</span>
              </button>
            ))}
          </div>
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
      >
        {mode === "demo" && (
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
                setEvent(item);
                if (!item.read)
                  act(`read-${item.id}`, async () => {
                    await post(`/events/${item.id}/read`, { read: true });
                    await reload();
                  });
              }}
              onSettings={() => setPanel("preferences")}
              onProfile={() => setPanel("profile")}
              onRun={openRun}
            />
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
                    对话与查到的资料，自动保存在本机
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
                      startRun(question, "auto", pendingMatch);
                    }}
                  >
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
                          startRun(question, "auto", pendingMatch);
                        }
                      }}
                    />
                    <div className="v4-composer-bottom">
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
          </div>
        </Modal>
      )}
      {event && (
        <Modal title="球队动态" onClose={closeEvent}>
          <div className="event-reading">
            <span className="quiet-label">{dateTime(event.updated_at)}</span>
            <h2>
              {chineseText(event.display_title || event.title, "球队最新消息")}
            </h2>
            <p>
              {chineseText(
                event.display_summary || event.summary || event.change,
                "已收录相关消息，可请助手整理中文要点。",
              )}
            </p>
            {event.uncertainties
              ?.filter((x) => /[\u3400-\u9fff]/.test(x))
              .map((text, index) => (
                <p className="event-uncertainty" key={index}>
                  {text}
                </p>
              ))}
            <div className="source-link-list">
              {event.evidence_ids
                .map((id) => evidence.find((e) => e.id === id))
                .filter((e): e is Evidence => !!e)
                .map((item) =>
                  safeUrl(item.url) ? (
                    <a
                      key={item.id}
                      href={safeUrl(item.url) || undefined}
                      target="_blank"
                      rel="noopener noreferrer"
                    >
                      {sourceLabel(item)}
                      <ArrowUpRight size={15} />
                    </a>
                  ) : (
                    <span className="quiet-label" key={item.id}>
                      {mode === "demo" ? "演示资料" : "本地导入资料"}
                    </span>
                  ),
                )}
            </div>
            <button
              className="button dark-button"
              onClick={() =>
                ask(
                  `解读“${event.display_title || chineseText(event.title)}”，对${team?.name}有什么影响？`,
                )
              }
            >
              <Sparkles size={16} />
              请助手解读
            </button>
          </div>
        </Modal>
      )}
      {document && (
        <Modal title="资料出处" onClose={closeDocument}>
          <EvidenceDetail item={document} />
        </Modal>
      )}
    </div>
  );
}
