import { useEffect, useRef, useState } from "react";
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
type ChatRun = Run & { provider_id?: string };
const cards = [
  {
    id: "team_brief",
    title: "今日球队速览",
    description: "重要消息、阵容变化与下一场比赛",
    icon: Sparkles,
    prompt: (team: string) =>
      `帮我整理${team}今天值得关注的消息：发生了什么，对球队有什么影响，接下来关注什么？`,
  },
  {
    id: "match_preview",
    title: "比赛前瞻",
    description: "看点、对位与可能左右比赛的细节",
    icon: Trophy,
    prompt: (team: string) =>
      `请为${team}下一场比赛做一份前瞻，介绍双方近况、关键对位和比赛看点。`,
  },
  {
    id: "match_review",
    title: "赛后复盘",
    description: "关键转折与比赛背后的原因",
    icon: ChartNoAxesCombined,
    prompt: (team: string) =>
      `请复盘${team}最近一场比赛，分析关键转折、战术调整和球员表现。`,
  },
  {
    id: "transfer_check",
    title: "转会追踪",
    description: "厘清传闻、已知进展与实际影响",
    icon: ScanSearch,
    prompt: (team: string) =>
      `帮我梳理${team}近期转会消息，区分官方确认和媒体传闻，分析可能对阵容产生的影响。`,
  },
  {
    id: "tactics_explainer",
    title: "战术讲解",
    description: "把阵型、跑位与配合理解透彻",
    icon: Shield,
    prompt: (team: string) =>
      `用容易理解的方式讲解${team}目前的战术风格、攻防套路与关键球员的作用。`,
  },
  {
    id: "team_maintenance",
    title: "球队与球员画像",
    description: "球队风格、球员特点与数据表现",
    icon: Users,
    prompt: (team: string) =>
      `为${team}做一份球队画像：球队背景、核心阵容、关键球员特点和本赛季数据表现。`,
  },
];
const activeRun = (run: Run) => ["queued", "running"].includes(run.status);
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
}: {
  team: Team;
  data: AppState;
  incoming: AssistantPrompt | null;
  openModel: () => void;
  onChanged: () => Promise<void>;
}) {
  const [question, setQuestion] = useState("");
  const [skill, setSkill] = useState("auto");
  const [context, setContext] = useState<AssistantPrompt | null>(null);
  const [conversationId, setConversationId] = useState<string | null>(null);
  const [conversationMatchId, setConversationMatchId] = useState<string | null>(
    null,
  );
  const [runs, setRuns] = useState<ChatRun[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [history, setHistory] = useState(false);
  const [modelMenu, setModelMenu] = useState(false);
  const [models, setModels] = useState<
    { provider_id: string; provider_name: string; id: string; name: string }[]
  >([]);
  const [selectedModel, setSelectedModel] = useState<{
    provider_id: string;
    model: string;
  } | null>(null);
  const textarea = useRef<HTMLTextAreaElement>(null);
  const bottom = useRef<HTMLDivElement>(null);
  const request = useRef(0);
  const mounted = useRef(true);
  const pending = runs.find(activeRun);
  const selectedCard = cards.find((item) => item.id === skill);
  const histories = data.runs
    .filter(
      (run) =>
        run.team_id === team.id &&
        run.mode === "live" &&
        !run.internal_report &&
        String(run.skill) !== "news_report",
    )
    .filter(
      (run, index, all) =>
        all.findIndex(
          (other) => other.conversation_id === run.conversation_id,
        ) === index,
    );
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      request.current++;
    };
  }, []);
  useEffect(() => {
    if (!incoming) return;
    request.current++;
    setRuns([]);
    setConversationId(null);
    setConversationMatchId(null);
    setBusy(false);
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
        if (!conversationId) setSelectedModel(result.active);
      })
      .catch(() => undefined);
    return () => {
      valid = false;
    };
  }, [data.settings.llm.model, data.settings.llm.base_url]);
  useEffect(() => {
    if (!pending) return;
    let valid = true;
    let timer: number;
    const tick = async () => {
      try {
        const next = await api<ChatRun>(
          `/runs/${encodeURIComponent(pending.id)}`,
        );
        if (!valid) return;
        setRuns((previous) =>
          previous.map((run) => (run.id === next.id ? next : run)),
        );
        if (activeRun(next)) timer = window.setTimeout(tick, 1600);
        else void onChanged();
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
  }, [pending?.id, onChanged]);
  useEffect(() => {
    bottom.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [runs.length]);
  useEffect(() => {
    if (!textarea.current) return;
    textarea.current.style.height = "auto";
    textarea.current.style.height = `${Math.min(160, textarea.current.scrollHeight)}px`;
  }, [question]);
  const start = async (retry?: ChatRun) => {
    const text = retry?.question || question.trim();
    if (!text || busy || pending) return;
    if (!data.setup?.model_configured) {
      openModel();
      return;
    }
    setBusy(true);
    setError("");
    const serial = ++request.current;
    try {
      const chosenSkill = retry?.skill || skill;
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
      const next = await post<ChatRun>("/runs", {
        mode: "live",
        team_id: team.id,
        question: text,
        skill: chosenSkill,
        allow_network: true,
        conversation_id: conversationId || undefined,
        match_id: retry?.match_id || match,
        evidence_ids: retry?.evidence_ids || context?.evidenceIds,
        provider_id: selectedModel?.provider_id,
        model: selectedModel?.model,
      });
      if (serial !== request.current || !mounted.current) return;
      setRuns((previous) => [...previous, next]);
      setConversationId(next.conversation_id);
      setConversationMatchId(next.match_id || null);
      setQuestion("");
      setContext(null);
      setSkill("auto");
      void onChanged();
    } catch (err) {
      if (serial === request.current && mounted.current)
        setError(errorText(err));
    } finally {
      if (mounted.current) setBusy(false);
    }
  };
  const cancel = async () => {
    if (!pending || busy) return;
    setBusy(true);
    try {
      const next = await post<ChatRun>(`/runs/${pending.id}/cancel`);
      if (mounted.current)
        setRuns((previous) =>
          previous.map((run) => (run.id === next.id ? next : run)),
        );
    } catch (err) {
      if (mounted.current) setError(errorText(err));
    } finally {
      if (mounted.current) setBusy(false);
    }
  };
  const loadConversation = async (run: Run) => {
    if (pending) return;
    setHistory(false);
    setBusy(true);
    setError("");
    const serial = ++request.current;
    try {
      const entries = data.runs
        .filter(
          (item) =>
            item.conversation_id === run.conversation_id &&
            item.team_id === team.id &&
            !item.internal_report,
        )
        .sort((a, b) => (a.created_at || "").localeCompare(b.created_at || ""));
      const detailed = await Promise.all(
        entries.map((item) =>
          api<ChatRun>(`/runs/${encodeURIComponent(item.id)}`),
        ),
      );
      if (serial !== request.current || !mounted.current) return;
      setRuns(detailed);
      setConversationId(run.conversation_id);
      setConversationMatchId(run.match_id || null);
      setQuestion("");
      setContext(null);
      setSkill("auto");
      if (detailed[0]?.provider_id && detailed[0]?.model)
        setSelectedModel({
          provider_id: detailed[0].provider_id,
          model: detailed[0].model,
        });
    } catch (err) {
      if (mounted.current) setError(errorText(err));
    } finally {
      if (mounted.current) setBusy(false);
    }
  };
  const newConversation = () => {
    if (pending) return;
    setRuns([]);
    setConversationId(null);
    setConversationMatchId(null);
    setQuestion("");
    setContext(null);
    setSkill("auto");
    setError("");
    textarea.current?.focus();
  };
  return (
    <div className="assistant-page">
      <div className="chat-page-heading">
        <div>
          <span className="eyebrow">YOUR FOOTBALL COMPANION</span>
          <h1>
            问助手
            <span className="heading-dot" />
          </h1>
        </div>
        <div className="chat-top-actions">
          <button
            className="icon-button"
            title="历史对话"
            aria-label="历史对话"
            onClick={() => setHistory(true)}
          >
            <History size={19} />
          </button>
          <button
            className="button"
            disabled={!!pending || busy}
            onClick={newConversation}
          >
            <Plus size={16} />
            新对话
          </button>
        </div>
      </div>
      {!data.setup?.model_configured && (
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
        {!runs.length && (
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
        {runs.map((run) => (
          <section className="conversation-turn" key={run.id}>
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
                  evidence={run.evidence || data.evidence}
                />
              ) : activeRun(run) ? (
                <div className="thinking-status">
                  <span className="thinking-dots">
                    <i />
                    <i />
                    <i />
                  </span>
                  <div>
                    正在查找资料、整理思路
                    <small>需要一点时间，可以先继续浏览其他页面。</small>
                  </div>
                </div>
              ) : null}
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
                      setQuestion(
                        `关于刚才的回答，请进一步解释其中最值得关注的变化。`,
                      );
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
        <div ref={bottom} />
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
            onChange={(event) => setQuestion(event.target.value)}
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
                disabled={
                  busy || !question.trim() || !data.setup?.model_configured
                }
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
            ? "本次对话使用当前模型，新对话可切换。"
            : "助手会查阅来源再回答。"}
          Enter 发送，Shift + Enter 换行。
        </p>
      </div>
      {history && (
        <Modal title="最近的对话" onClose={() => setHistory(false)}>
          <div className="chat-history">
            {histories.map((run) => (
              <button
                key={run.id}
                disabled={!!pending || busy}
                onClick={() => loadConversation(run)}
              >
                <MessageCircle size={18} />
                <span>
                  <strong>{run.question}</strong>
                  <small>{dateTime(run.created_at)}</small>
                </span>
                <ChevronRight size={16} />
              </button>
            ))}
            {!histories.length && (
              <div className="empty-state">
                <Clock3 size={30} />
                <p>你的对话会留在这里，随时可以继续。</p>
              </div>
            )}
            {pending && (
              <p className="muted">请等待当前回答完成，或先停止回答。</p>
            )}
          </div>
        </Modal>
      )}
    </div>
  );
}
