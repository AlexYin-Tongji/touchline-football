import { useCallback, useEffect, useRef, useState } from "react";
import {
  ArrowRight,
  ArrowUp,
  ChartNoAxesCombined,
  Check,
  ChevronDown,
  ChevronRight,
  Clock3,
  History,
  LoaderCircle,
  MessageCircle,
  Plus,
  RotateCcw,
  ScanSearch,
  Search,
  Shield,
  Sparkles,
  Square,
  Trophy,
  Users,
  X,
} from "lucide-react";
import { api, errorText, post } from "./api";
import { dateTime, Modal } from "./components";
import { Markdown, Sources } from "./v7Shared";
import type { AppState, Run, Team } from "./types";

export interface AssistantPrompt {
  text: string;
  skill?: string;
  matchId?: string;
  evidenceIds?: string[];
  token?: number;
}
type AssistantContext = Pick<AssistantPrompt, "matchId" | "evidenceIds">;
interface AssistantDraft {
  text: string;
  skill: string;
  context: AssistantContext | null;
  conversationId: string | null;
}
type ChatRun = Run & { provider_id?: string };
interface ConversationRecord {
  id: string;
  team_id: string;
  title: string;
  created_at?: string;
  updated_at?: string;
  run_count: number;
  last_status: string;
  match_id?: string | null;
  provider_id?: string;
  model?: string;
}
type ModelSelection = { provider_id: string; model: string };
const savedValue = (key: string) => {
  try {
    return localStorage.getItem(key) || "";
  } catch {
    return "";
  }
};
const remember = (key: string, value: string) => {
  try {
    localStorage.setItem(key, value);
  } catch {
    /* Server history remains available. */
  }
};
const conversationStatus: Record<string, string> = {
  queued: "等待回答",
  running: "回答中",
  completed: "已完成",
  failed: "可重试",
  cancelled: "已停止",
};
// Each card writes a one-line question into the composer. They used to spell out
// three or four enumeration clauses ("发生了什么，对球队有什么影响，接下来关注什么"),
// which pre-loaded every turn with a four-part brief and was a large part of why
// ordinary answers arrived as a filled-in form: the model answered the shape of
// the question rather than the question. One clause each, phrased the way a
// supporter would actually ask it.
const cards = [
  {
    id: "team_brief",
    title: "今日球队速览",
    description: "重要消息、阵容变化与下一场比赛",
    icon: Sparkles,
    prompt: (team: string) => `${team}今天有什么值得关注的消息？`,
  },
  {
    id: "match_preview",
    title: "比赛前瞻",
    description: "看点、对位与可能左右比赛的细节",
    icon: Trophy,
    prompt: (team: string) => `${team}下一场有什么值得留意的？`,
  },
  {
    id: "match_review",
    title: "赛后复盘",
    description: "关键转折与比赛背后的原因",
    icon: ChartNoAxesCombined,
    prompt: (team: string) => `${team}最近这场比赛是怎么输赢的？`,
  },
  {
    id: "transfer_check",
    title: "转会追踪",
    description: "厘清传闻、已知进展与实际影响",
    icon: ScanSearch,
    prompt: (team: string) => `${team}最近的转会消息有哪些是靠得住的？`,
  },
  {
    id: "tactics_explainer",
    title: "战术讲解",
    description: "把阵型、跑位与配合理解透彻",
    icon: Shield,
    prompt: (team: string) => `${team}现在踢的是什么路子？`,
  },
  {
    id: "team_maintenance",
    title: "球队与球员画像",
    description: "球队风格、球员特点与数据表现",
    icon: Users,
    prompt: (team: string) => `给我讲讲${team}现在这套阵容。`,
  },
];
const activeRun = (run: Run) => ["queued", "running"].includes(run.status);
function restoreDraft(teamId: string): AssistantDraft {
  const text = savedValue(`touchline-draft-${teamId}`);
  const conversationId = savedValue(`touchline-conversation-${teamId}`) || null;
  const fallback = { text, skill: "auto", context: null, conversationId };
  if (!text.trim()) return fallback;
  try {
    const saved = JSON.parse(savedValue(`touchline-draft-context-${teamId}`));
    if (
      saved?.version !== 1 ||
      saved.team_id !== teamId ||
      saved.conversation_id !== conversationId
    )
      return fallback;
    const skill = [
      "auto",
      "ask",
      "event_watch",
      ...cards.map((card) => card.id),
    ].includes(saved.skill)
      ? saved.skill
      : "auto";
    const matchId =
      typeof saved.match_id === "string" && saved.match_id.length <= 200
        ? saved.match_id
        : undefined;
    const evidenceIds = Array.isArray(saved.evidence_ids)
      ? saved.evidence_ids
          .filter(
            (id: unknown): id is string =>
              typeof id === "string" && id.length > 0 && id.length <= 200,
          )
          .slice(0, 5)
      : undefined;
    return {
      text,
      skill,
      conversationId,
      context: matchId || evidenceIds?.length ? { matchId, evidenceIds } : null,
    };
  } catch {
    return fallback;
  }
}
function responseError(value?: string | null) {
  if (!value) return "这次回答没有完成，可以重试。";
  if (/401|403|密钥|api.?key|未.*配置|未.*设置/i.test(value))
    return "模型连接尚未就绪，请检查模型配置后重试。";
  if (/timeout|超时|连接|网络/i.test(value))
    return "连接暂时中断了，可以重新尝试。";
  return "这次查询没有完成，请重试或换一种方式提问。";
}

export function Assistant({
  team,
  data,
  incoming,
  openModel,
  onChanged,
  active = true,
  modelRevision = 0,
}: {
  team: Team;
  data: AppState;
  incoming: AssistantPrompt | null;
  openModel: () => void;
  onChanged: () => Promise<void>;
  active?: boolean;
  modelRevision?: number;
}) {
  const [initialDraft] = useState(() => restoreDraft(team.id));
  const [question, setQuestion] = useState(initialDraft.text);
  const [skill, setSkill] = useState(initialDraft.skill);
  const [context, setContext] = useState<AssistantContext | null>(
    initialDraft.context,
  );
  const [conversationId, setConversationId] = useState<string | null>(null);
  const [conversationMatchId, setConversationMatchId] = useState<string | null>(
    null,
  );
  const [runs, setRuns] = useState<ChatRun[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [history, setHistory] = useState(false);
  const [histories, setHistories] = useState<ConversationRecord[]>([]);
  const [historyCursor, setHistoryCursor] = useState<string | null>(null);
  const [historyBusy, setHistoryBusy] = useState(false);
  const [historyQuery, setHistoryQuery] = useState("");
  const [historyError, setHistoryError] = useState("");
  const [restoring, setRestoring] = useState(false);
  const [modelMenu, setModelMenu] = useState(false);
  const [modelsLoaded, setModelsLoaded] = useState(false);
  const [models, setModels] = useState<
    { provider_id: string; provider_name: string; id: string; name: string }[]
  >([]);
  const [selectedModel, setSelectedModel] = useState<{
    provider_id: string;
    model: string;
  } | null>(null);
  const [roleModels, setRoleModels] = useState<
    Partial<Record<"chat" | "team", ModelSelection | null>>
  >({});
  const textarea = useRef<HTMLTextAreaElement>(null);
  const lastTurn = useRef<HTMLElement | null>(null);
  const prevRuns = useRef(0);
  const request = useRef(0);
  const historyRequest = useRef(0);
  const mounted = useRef(true);
  const modelOverride = useRef(false);
  const currentConversation = useRef(conversationId);
  const draftConversation = useRef(initialDraft.conversationId);
  const currentDraft = useRef(question);
  currentDraft.current = question;
  currentConversation.current = conversationId;
  const pending = runs.find(activeRun);
  const selectedCard = cards.find((item) => item.id === skill);
  const missingConversationModel = !!conversationId && !!selectedModel && modelsLoaded &&
    !models.some((model) => model.provider_id === selectedModel.provider_id && model.id === selectedModel.model);
  const modelReady =
    !missingConversationModel && ((modelOverride.current && !!selectedModel) ||
    (conversationId && !!selectedModel) ||
    (data.setup?.model_roles_configured?.[
      skill === "team_maintenance" ? "team" : "chat"
    ] ??
      data.setup?.model_configured));
  const loadHistory = useCallback(
    async (cursor?: string) => {
      const serial = ++historyRequest.current;
      setHistoryBusy(true);
      try {
        const result = await api<{
          conversations: ConversationRecord[];
          next_cursor?: string | null;
        }>(
          `/conversations?mode=live&team_id=${encodeURIComponent(team.id)}&limit=30${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}`,
        );
        if (!mounted.current || serial !== historyRequest.current) return;
        setHistories((previous) =>
          cursor
            ? [
                ...previous,
                ...result.conversations.filter(
                  (item) => !previous.some((old) => old.id === item.id),
                ),
              ]
            : result.conversations,
        );
        setHistoryCursor(result.next_cursor || null);
        setHistoryError("");
      } catch (err) {
        if (mounted.current && serial === historyRequest.current)
          setHistoryError(errorText(err));
      } finally {
        if (mounted.current && serial === historyRequest.current)
          setHistoryBusy(false);
      }
    },
    [team.id],
  );
  const loadConversation = useCallback(
    async (id: string, preserveDraft = false) => {
      setBusy(true);
      setError("");
      const serial = ++request.current;
      try {
        const result = await api<{
          conversation: ConversationRecord;
          runs: ChatRun[];
        }>(`/conversations/${encodeURIComponent(id)}`);
        if (serial !== request.current || !mounted.current) return;
        if (result.conversation.team_id !== team.id)
          throw new Error("该对话属于另一支球队，请先切换球队。");
        setRuns(result.runs);
        currentConversation.current = id;
        setConversationId(id);
        setConversationMatchId(
          result.conversation.match_id ||
            [...result.runs].reverse().find((run) => run.match_id)?.match_id ||
            null,
        );
        remember(`touchline-conversation-${team.id}`, id);
        draftConversation.current = id;
        if (!preserveDraft) {
          setQuestion("");
          setContext(null);
          setSkill("auto");
        }
        setHistory(false);
        const saved = result.conversation;
        const first = result.runs[0];
        if (saved.provider_id && saved.model)
          setSelectedModel({
            provider_id: saved.provider_id,
            model: saved.model,
          });
        else if (first?.provider_id && first?.model)
          setSelectedModel({
            provider_id: first.provider_id,
            model: first.model,
          });
      } catch (err) {
        if (mounted.current && serial === request.current)
          setError(errorText(err));
      } finally {
        if (mounted.current && serial === request.current) {
          setBusy(false);
          setRestoring(false);
        }
      }
    },
    [team.id],
  );
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      request.current++;
    };
  }, []);
  useEffect(() => {
    void loadHistory();
    const saved = savedValue(`touchline-conversation-${team.id}`);
    if (saved && !incoming) {
      setRestoring(true);
      void loadConversation(saved, true);
    }
  }, [team.id, loadHistory, loadConversation]);
  useEffect(() => {
    remember(`touchline-draft-${team.id}`, question);
    remember(
      `touchline-draft-context-${team.id}`,
      question.trim()
        ? JSON.stringify({
            version: 1,
            team_id: team.id,
            conversation_id: draftConversation.current,
            skill,
            match_id: context?.matchId,
            evidence_ids: context?.evidenceIds?.slice(0, 5),
          })
        : "",
    );
  }, [question, skill, context, conversationId, team.id]);
  useEffect(() => {
    if (history) void loadHistory();
  }, [history, loadHistory]);
  useEffect(() => {
    if (!incoming) return;
    request.current++;
    setRuns([]);
    currentConversation.current = null;
    setConversationId(null);
    setConversationMatchId(null);
    setBusy(false);
    setRestoring(false);
    modelOverride.current = false;
    remember(`touchline-conversation-${team.id}`, "");
    draftConversation.current = null;
    setError("");
    setQuestion(incoming.text);
    setSkill(incoming.skill || "auto");
    setContext(incoming);
    textarea.current?.focus();
  }, [incoming?.token]);
  useEffect(() => {
    let valid = true;
    api<{
      providers: {
        id: string;
        name: string;
        models: { id: string; name: string }[];
      }[];
      active: { provider_id: string; model: string } | null;
      resolved_roles?: Partial<Record<"chat" | "team", ModelSelection | null>>;
    }>("/model-providers")
      .then((result) => {
        if (!valid) return;
        setModels(
          result.providers.flatMap((provider) =>
            provider.models.map((model) => ({
              ...model,
              provider_id: provider.id,
              provider_name: provider.name,
            })),
          ),
        );
        setModelsLoaded(true);
        setRoleModels({
          chat: result.resolved_roles?.chat || result.active,
          team: result.resolved_roles?.team || result.active,
        });
        if (!currentConversation.current && !modelOverride.current)
          setSelectedModel(
            (skill === "team_maintenance"
              ? result.resolved_roles?.team
              : result.resolved_roles?.chat) || result.active,
          );
      })
      .catch(() => undefined);
    return () => {
      valid = false;
    };
   }, [modelRevision]);
  useEffect(() => {
    if (!conversationId && !modelOverride.current)
      setSelectedModel(
        (skill === "team_maintenance" ? roleModels.team : roleModels.chat) ||
          null,
      );
  }, [skill, conversationId, roleModels]);
  useEffect(() => {
    if (!pending || !active) return;
    let valid = true;
    let timer: number;
    const tick = async () => {
      if (document.visibilityState !== "visible") {
        timer = window.setTimeout(tick, 5000);
        return;
      }
      try {
        const next = await api<ChatRun>(
          `/runs/${encodeURIComponent(pending.id)}`,
        );
        if (!valid) return;
        setRuns((previous) =>
          previous.map((run) => (run.id === next.id ? next : run)),
        );
        if (activeRun(next)) timer = window.setTimeout(tick, 1600);
        else {
          void onChanged();
          void loadHistory();
        }
      } catch (err) {
        if (valid) {
          setError(errorText(err));
          timer = window.setTimeout(tick, 5000);
        }
      }
    };
    timer = window.setTimeout(tick, 900);
    return () => {
      valid = false;
      clearTimeout(timer);
    };
  }, [pending?.id, active, onChanged, loadHistory]);
  useEffect(() => {
    // 只在新增一轮问答时滚动，并且滚到这一轮的开头：
    // 滚到底会把刚生成的长回答直接推上去，反而看不到开头。
    const added = runs.length > prevRuns.current;
    prevRuns.current = runs.length;
    if (!added) return;
    lastTurn.current?.scrollIntoView({ behavior: "smooth", block: "start" });
  }, [runs.length]);
  useEffect(() => {
    if (!textarea.current) return;
    textarea.current.style.height = "auto";
    textarea.current.style.height = `${Math.min(160, textarea.current.scrollHeight)}px`;
  }, [question]);
  const start = async (retry?: ChatRun) => {
    const text = retry?.question || question.trim();
    if (!text || busy || pending) return;
    if (!modelReady) {
      openModel();
      return;
    }
    setBusy(true);
    setError("");
    const serial = ++request.current;
    const submittedDraft = question;
    try {
      const chosenSkill = retry?.skill || skill;
      const chosenModel =
        conversationId || modelOverride.current
          ? selectedModel
          : chosenSkill === "team_maintenance"
            ? roleModels.team
            : roleModels.chat;
      const match =
        context?.matchId ||
        conversationMatchId ||
        (chosenSkill === "match_preview"
          ? data.matches
              .filter(
                (item) =>
                  item.team_id === team.id &&
                  item.status !== "finished" &&
                  item.kickoff &&
                  Date.parse(item.kickoff) > Date.now(),
              )
              .sort((a, b) =>
                (a.kickoff || "").localeCompare(b.kickoff || ""),
              )[0]?.id
          : chosenSkill === "match_review"
            ? data.matches
                .filter(
                  (item) =>
                    item.team_id === team.id && item.status === "finished",
                )
                .sort((a, b) =>
                  (b.kickoff || "").localeCompare(a.kickoff || ""),
                )[0]?.id
            : undefined);
      const next = retry
        ? await post<ChatRun>(`/runs/${encodeURIComponent(retry.id)}/resume`)
        : await post<ChatRun>("/runs", {
            mode: "live",
            team_id: team.id,
            question: text,
            skill: chosenSkill,
            allow_network: true,
            conversation_id: conversationId || undefined,
            match_id: match,
            evidence_ids: context?.evidenceIds,
            provider_id: chosenModel?.provider_id,
            model: chosenModel?.model,
          });
      if (serial !== request.current || !mounted.current) return;
      setRuns((previous) => [...previous, next]);
      currentConversation.current = next.conversation_id;
      setConversationId(next.conversation_id);
      if (next.provider_id && next.model)
        setSelectedModel({ provider_id: next.provider_id, model: next.model });
      remember(`touchline-conversation-${team.id}`, next.conversation_id);
      draftConversation.current = next.conversation_id;
      setConversationMatchId(next.match_id || null);
      if (!retry && currentDraft.current === submittedDraft) {
        setQuestion("");
        setContext(null);
        setSkill("auto");
      }
      void onChanged();
      void loadHistory();
    } catch (err) {
      if (serial === request.current && mounted.current)
        setError(errorText(err));
    } finally {
      if (mounted.current && serial === request.current) setBusy(false);
    }
  };
  const cancel = async () => {
    if (!pending || busy) return;
    setBusy(true);
    const serial = request.current;
    try {
      const next = await post<ChatRun>(`/runs/${pending.id}/cancel`);
      if (mounted.current && serial === request.current)
        setRuns((previous) =>
          previous.map((run) => (run.id === next.id ? next : run)),
        );
      void loadHistory();
      void onChanged();
    } catch (err) {
      if (mounted.current && serial === request.current)
        setError(errorText(err));
    } finally {
      if (mounted.current && serial === request.current) setBusy(false);
    }
  };
  const newConversation = (preserveDraft = false) => {
    request.current++;
    remember(`touchline-conversation-${team.id}`, "");
    draftConversation.current = null;
    modelOverride.current = false;
    setSelectedModel(roleModels.chat || null);
    setRuns([]);
    currentConversation.current = null;
    setConversationId(null);
    setConversationMatchId(null);
    if (!preserveDraft) setQuestion("");
    setContext(null);
    setSkill("auto");
    setError("");
    setBusy(false);
    setRestoring(false);
    textarea.current?.focus();
  };
  return (
    <div className="assistant-page">
      <div className="chat-page-heading">
        <div>
          <h1>
            问助手
            <span className="heading-dot" />
          </h1>
        </div>
        <div className="chat-top-actions">
          <button
            className="button history-toggle"
            title="历史对话"
            aria-label="历史对话"
            onClick={() => setHistory(true)}
          >
            <History size={19} />
            <span>对话记录</span>
          </button>
          <button className="button" disabled={busy} onClick={() => newConversation()}>
            <Plus size={16} />
            新对话
          </button>
        </div>
      </div>
      <div className="assistant-body">
        <div className="conversation-context">
          <span>
            <Shield size={13} /> {team.name} ·{" "}
            {conversationId ? "会话已保存" : "独立新对话"}
          </span>
          <span>资料随提问更新 · 仅延续当前会话</span>
        </div>
        {!runs.length && histories.length > 0 && !restoring && (
          <div className="resume-conversation">
            <History size={17} />
            <div>
              <small>上次聊到</small>
              <strong>{histories[0].title}</strong>
            </div>
            <button
              className="text-button"
              disabled={busy}
              onClick={() => loadConversation(histories[0].id)}
            >
              继续对话 <ArrowRight size={14} />
            </button>
          </div>
        )}
        {missingConversationModel && (
          <div className="model-recovery" role="status">
            <div>
              <strong>这段对话使用的模型已移除</strong>
              <p>历史记录仍然保留。你可以在模型配置中恢复原模型，或使用当前模型继续一个新对话。</p>
            </div>
            <button className="button primary" onClick={() => newConversation(true)}>保留问题，开启新对话</button>
            <button className="text-button" onClick={openModel}>打开模型配置</button>
          </div>
        )}
        {!modelReady && !missingConversationModel && (
          <div className="model-connect">
            <span>
              <Sparkles size={18} />
              连接你的模型，开启第一场足球对话。
            </span>
            <button onClick={openModel}>
              配置模型
              <ArrowRight size={15} />
            </button>
          </div>
        )}
        <div className="conversation">
          {restoring && (
            <div className="thinking-status">
              <LoaderCircle size={18} className="spin" />
              正在恢复完整对话记录
            </div>
          )}
          {!runs.length && !restoring && (
            <section className="assistant-welcome">
              <span className="welcome-mark">
                <Sparkles size={28} />
              </span>
              <h2>今天，想聊点什么？</h2>
              <p>
                关于{team.name}，从一条消息到一场比赛，
                <br className="desktop-only" />
                我会带着资料，陪你一起看懂。
              </p>
              <div className="skill-grid">
                {cards.map((card) => (
                  <button
                    className={skill === card.id ? "selected" : ""}
                    key={card.id}
                    onClick={() => {
                      setSkill(card.id);
                      setContext(null);
                      setQuestion(card.prompt(team.name));
                      textarea.current?.focus();
                    }}
                  >
                    <span className={`skill-icon skill-${card.id}`}>
                      <card.icon size={19} />
                    </span>
                    <div>
                      <strong>{card.title}</strong>
                      <small>{card.description}</small>
                    </div>
                    <ArrowRight size={15} />
                  </button>
                ))}
              </div>
            </section>
          )}
          {runs.map((run, index) => (
            <section
              className="conversation-turn"
              key={run.id}
              ref={index === runs.length - 1 ? lastTurn : undefined}
            >
              <div className="user-bubble">{run.question}</div>
              <article className="assistant-answer">
                <div className="answer-byline">
                  <span>
                    <Sparkles size={17} />
                  </span>
                  <strong>看台助手</strong>
                  {run.model && <small>{run.model}</small>}
                </div>
                {run.answer ? (
                  <Markdown
                    text={run.answer}
                    evidence={run.evidence || []}
                  />
                ) : activeRun(run) ? (
                  <div className="thinking-status">
                    <span className="thinking-dots">
                      <i />
                      <i />
                      <i />
                    </span>
                    <div>
                      {run.steps
                        ?.filter((step) => step.status === "running")
                        .slice(-1)[0]?.label || "正在查找资料、整理思路"}
                      <small>需要一点时间，可以先继续浏览其他页面。</small>
                    </div>
                  </div>
                ) : null}
                {!!run.steps?.length && (
                  <details className="answer-process">
                    <summary>
                      <ScanSearch size={13} />
                      资料查阅过程 <span>{run.steps.length} 项进展</span>
                      <ChevronDown size={13} />
                    </summary>
                    <ol>
                      {run.steps.map((step, index) => (
                        <li
                          key={`${index}-${step.at}`}
                          className={`step-${step.status}`}
                        >
                          <span className="process-dot" />
                          <span>{step.label}</span>
                          <small>
                            {(
                              {
                                completed: "完成",
                                success: "完成",
                                failed: "未完成",
                                error: "未完成",
                                running: "进行中",
                                skipped: "跳过",
                              } as Record<string, string>
                            )[step.status] || step.status}
                          </small>
                        </li>
                      ))}
                    </ol>
                  </details>
                )}
                {run.status === "failed" && (
                  <div className="notice error">
                    <span>{responseError(run.error)}</span>
                    <button
                      disabled={!!pending || busy}
                      onClick={() => start(run)}
                    >
                      <RotateCcw size={14} />
                      重试
                    </button>
                  </div>
                )}
                {run.status === "cancelled" && (
                  <p className="muted">
                    回答已停止。
                    <button
                      className="text-button"
                      disabled={!!pending || busy}
                      onClick={() => start(run)}
                    >
                      重新回答
                    </button>
                  </p>
                )}
                {run.evidence?.length ? <Sources sources={run.evidence} /> : null}
                {run.status === "completed" && (
                  <div className="answer-footer">
                    <span>{dateTime(run.finished_at || run.created_at)}</span>
                    <button
                      className="text-button"
                      onClick={() => {
                        // Asked the way a person would ask it. The previous fixed
                        // text ("请进一步解释其中最值得关注的变化") was abstract enough
                        // that no source could answer it directly, so every
                        // follow-up produced the same generalising paragraph.
                        setQuestion("这里面哪一条最要紧？");
                        textarea.current?.focus();
                      }}
                    >
                      <MessageCircle size={14} />
                      继续聊聊
                    </button>
                  </div>
                )}
              </article>
            </section>
          ))}
        </div>
        <div className="composer-area">
          {error && (
            <div className="notice error" role="alert">
              {error}
              <button aria-label="关闭错误提示" onClick={() => setError("")}>
                <X size={15} />
              </button>
            </div>
          )}
          <form
            className="composer"
            onSubmit={(event) => {
              event.preventDefault();
              void start();
            }}
          >
            {skill !== "auto" && (
              <div className="composer-context">
                <Sparkles size={13} />
                <span>
                  {selectedCard?.title ||
                    (skill === "team_maintenance" ? "球员画像" : "相关消息")}
                </span>
                <button
                  type="button"
                  aria-label="取消已选任务"
                  onClick={() => {
                    setSkill("auto");
                    setContext(null);
                  }}
                >
                  <X size={13} />
                </button>
              </div>
            )}
            <textarea
              ref={textarea}
              aria-label="向足球助手提问"
              placeholder={`关于${team.name}，你想知道什么？`}
              value={question}
              maxLength={12000}
              rows={2}
              onChange={(event) => {
                currentDraft.current = event.target.value;
                setQuestion(event.target.value);
              }}
              onKeyDown={(event) => {
                if (
                  event.key === "Enter" &&
                  !event.shiftKey &&
                  !event.nativeEvent.isComposing
                ) {
                  event.preventDefault();
                  if (!pending) void start();
                }
              }}
            />
            <div className="composer-footer">
              <div
                className="chat-model-picker"
                title={
                  conversationId
                    ? "本次对话使用当前模型。新建对话后可选择其他模型。"
                    : undefined
                }
              >
                <button
                  type="button"
                  className="text-button"
                  onClick={() => {
                    if (conversationId) return;
                    setModelMenu(!modelMenu);
                  }}
                  disabled={!!conversationId || !!pending}
                >
                  <Sparkles size={14} />
                  <span>
                    {selectedModel?.model ||
                      data.settings.llm.model ||
                      "选择模型"}
                  </span>
                  {!conversationId && <ChevronDown size={13} />}
                </button>
                {modelMenu && (
                  <div className="chat-model-menu">
                    {models.map((model) => (
                      <button
                        type="button"
                        key={`${model.provider_id}/${model.id}`}
                        onClick={() => {
                          modelOverride.current = true;
                          setSelectedModel({
                            provider_id: model.provider_id,
                            model: model.id,
                          });
                          setModelMenu(false);
                        }}
                      >
                        <span>
                          {model.name || model.id}
                          <small>{model.provider_name}</small>
                        </span>
                        {selectedModel?.provider_id === model.provider_id &&
                          selectedModel.model === model.id && <Check size={14} />}
                      </button>
                    ))}
                    <button
                      type="button"
                      className="manage-models"
                      onClick={() => {
                        setModelMenu(false);
                        openModel();
                      }}
                    >
                      管理模型
                      <ChevronRight size={14} />
                    </button>
                  </div>
                )}
              </div>
              {pending ? (
                <button
                  type="button"
                  className="send-button stop"
                  disabled={busy}
                  onClick={cancel}
                  aria-label="停止回答"
                >
                  <Square size={17} fill="currentColor" />
                </button>
              ) : (
                <button
                  className="send-button"
                  type="submit"
                  disabled={busy || !question.trim() || !modelReady}
                  aria-label="发送问题"
                >
                  {busy ? (
                    <LoaderCircle size={18} className="spin" />
                  ) : (
                    <ArrowUp size={21} />
                  )}
                </button>
              )}
            </div>
          </form>
          <p className="composer-note">
            {conversationId
              ? "对话自动保存；本次对话继续使用当前模型。"
              : "新对话不读取其他会话。助手会查阅来源再回答。"}
            Enter 发送，Shift + Enter 换行。
          </p>
        </div>
      </div>
      {history && (
        <Modal
          title={`${team.name} · 对话记录`}
          onClose={() => setHistory(false)}
          wide
        >
          <div className="chat-history">
            <p className="history-intro">
              完整记录按球队保存，切换页面、刷新后仍可继续。当前正在生成的回答也会留在记录中。
            </p>
            <label className="search-input">
              <Search size={17} />
              <input
                aria-label="搜索对话记录"
                placeholder="搜索已加载对话"
                value={historyQuery}
                onChange={(event) => setHistoryQuery(event.target.value)}
              />
            </label>
            {historyError && (
              <div className="notice error">
                {historyError}
                <button onClick={() => loadHistory()}>重试</button>
              </div>
            )}
            <div className="history-table-heading">
              <span>对话</span>
              <span>状态 / 更新时间</span>
            </div>
            {histories
              .filter((run) =>
                run.title.toLowerCase().includes(historyQuery.toLowerCase()),
              )
              .map((run) => (
                <button
                  key={run.id}
                  className={run.id === conversationId ? "active" : ""}
                  disabled={busy}
                  onClick={() => loadConversation(run.id)}
                >
                  <MessageCircle size={18} />
                  <span>
                    <strong>{run.title || "足球对话"}</strong>
                    <small>
                      {run.run_count} 轮对话{run.model ? ` · ${run.model}` : ""}
                    </small>
                  </span>
                  <span className="history-meta">
                    <b
                      className={`conversation-status status-${run.last_status}`}
                    >
                      {conversationStatus[run.last_status] || run.last_status}
                    </b>
                    <small>{dateTime(run.updated_at || run.created_at)}</small>
                  </span>
                  <ChevronRight size={16} />
                </button>
              ))}
            {!histories.length && !historyBusy && !historyError && (
              <div className="empty-state">
                <Clock3 size={30} />
                <p>你的对话会留在这里，随时可以继续。</p>
              </div>
            )}
            {historyBusy && (
              <p className="history-loading">
                <LoaderCircle size={15} className="spin" /> 正在读取对话记录
              </p>
            )}
            {historyCursor && (
              <button
                className="button history-more"
                disabled={historyBusy}
                onClick={() => loadHistory(historyCursor)}
              >
                更早的对话 <ChevronDown size={14} />
              </button>
            )}
          </div>
        </Modal>
      )}
    </div>
  );
}
