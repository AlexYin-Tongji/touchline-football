import { useEffect, useRef, useState, type FormEvent } from "react";
import {
  ArrowRight,
  ArrowUpRight,
  BookOpen,
  Check,
  CheckCircle2,
  ChevronDown,
  CircleAlert,
  Clock3,
  EyeOff,
  FileText,
  KeyRound,
  LoaderCircle,
  Plus,
  RefreshCw,
  Rss,
  Save,
  Server,
  Trash2,
  Wifi,
} from "lucide-react";
import { api, errorText, patch, post, put } from "./api";
import {
  Crest,
  dateTime,
  evidenceTitle,
  Empty,
  ErrorNotice,
  safeUrl,
  sourceLabel,
  Spinner,
} from "./components";
import { ResearchPanel } from "./v2pages";
import { AutomationSettings } from "./v3pages";
import type {
  AppState,
  ModelTest,
  ClubEvent,
  Evidence,
  Match,
  Mode,
  RefreshResult,
  Settings,
  Source,
  Team,
} from "./types";

type Notice = (message: string, error?: boolean) => void;

export function SourcesPage({
  data,
  teamId,
  mode,
  reload,
  notice,
  switchLive,
  openEvidence,
}: {
  data: AppState;
  teamId: string;
  mode: Mode;
  reload: () => Promise<void>;
  notice: Notice;
  switchLive: () => void;
  openEvidence: (e: Evidence) => void;
}) {
  const [tab, setTab] = useState<"sources" | "import">("sources");
  const [adding, setAdding] = useState(false);
  const [managing, setManaging] = useState(false);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [sourceName, setSourceName] = useState("");
  const [sourceUrl, setSourceUrl] = useState("");
  const [sourceKind, setSourceKind] = useState<"rss" | "html">("rss");
  const sources = data.sources.filter((x) => x.team_id === teamId && x.mode === mode);
  const documents = data.evidence.filter((x) => x.team_id === teamId && x.mode === mode);
  async function act(key: string, work: () => Promise<void>) {
    if (busy) return;
    setBusy(key);
    setError("");
    try {
      await work();
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy("");
    }
  }
  async function addSource(event: FormEvent) {
    event.preventDefault();
    await act("add", async () => {
      await post<Source>("/sources", {
        team_id: teamId,
        name: sourceName.trim(),
        url: sourceUrl.trim(),
        kind: sourceKind,
        enabled: true,
      });
      setAdding(false);
      setSourceName("");
      setSourceUrl("");
      await reload();
      notice("来源已添加。");
    });
  }
  async function refreshSource(id: string) {
    await act(id, async () => {
      const result = await post<RefreshResult>(`/sources/${id}/refresh`);
      await reload();
      if (result.errors?.length) setError("部分资料未能更新，请稍后重试或检查来源地址。");
      notice(
        result.added ? `已保存 ${result.added} 份新资料。` : result.errors?.length ? "资料更新未完成。" : "资料已是最新。",
      );
    });
  }
  return (
    <>
      <div className="page-title">
        <div>
          <h1>资料来源</h1>
        </div>
        {mode === "live" && (
          <button
            className="button primary"
            onClick={() => {
              setAdding(!adding);
              setManaging(true);
              setTab("sources");
            }}
          >
            <Plus size={17} />
            {adding ? "收起表单" : "添加来源"}
          </button>
        )}
      </div>
      {mode === "demo" && (
        <div className="mode-callout">
          <BookOpen size={22} />
          <div>
            <strong>当前展示虚构示例</strong>
          </div>
          <button className="button" onClick={switchLive}>
            使用真实资料 <ArrowRight size={15} />
          </button>
        </div>
      )}
      {mode === "live" && (
        <ResearchPanel
          key={teamId}
          teamId={teamId}
          reload={reload}
          notice={notice}
          openEvidence={openEvidence}
        />
      )}
      <details
        className="source-management"
        open={managing}
        onToggle={(e) => setManaging(e.currentTarget.open)}
      >
        <summary>
          订阅管理与手动导入 <span>{sources.length} 个自定义来源</span>
          <ChevronDown size={15} />
        </summary>
        <div className="segmented">
          <button
            className={tab === "sources" ? "active" : ""}
            onClick={() => setTab("sources")}
          >
            <Rss size={15} />
            订阅来源
          </button>
          <button
            className={tab === "import" ? "active" : ""}
            onClick={() => setTab("import")}
          >
            <FileText size={15} />
            手动导入
          </button>
        </div>
        <ErrorNotice>{error}</ErrorNotice>
        {tab === "sources" ? (
          <>
            {mode === "live" && adding && (
              <form className="panel form-panel" onSubmit={addSource}>
                <h2>添加一个来源</h2>
                <div className="form-grid">
                  <label>
                    来源名称
                    <input
                      required
                      maxLength={120}
                      value={sourceName}
                      onChange={(e) => setSourceName(e.target.value)}
                      placeholder="例如：俱乐部新闻"
                    />
                  </label>
                  <label>
                    来源类型
                    <select
                      value={sourceKind}
                      onChange={(e) =>
                        setSourceKind(e.target.value as "rss" | "html")
                      }
                    >
                      <option value="rss">新闻订阅</option>
                      <option value="html">文章网页</option>
                    </select>
                  </label>
                  <label className="full">
                    来源地址
                    <input
                      type="url"
                      required
                      value={sourceUrl}
                      onChange={(e) => setSourceUrl(e.target.value)}
                      placeholder="https://…"
                    />
                    <span className="field-hint">
                      填写订阅地址或具体文章的链接。
                    </span>
                  </label>
                </div>
                <div className="form-actions">
                  <button
                    className="button"
                    type="button"
                    onClick={() => setAdding(false)}
                  >
                    取消
                  </button>
                  <button className="button primary" disabled={!!busy}>
                    {busy === "add" ? (
                      <Spinner text="添加中" />
                    ) : (
                      <>
                        <Plus size={16} />
                        添加来源
                      </>
                    )}
                  </button>
                </div>
              </form>
            )}
            <div className="section-heading">
              <h2>
                当前球队的来源 <span className="count">{sources.length}</span>
              </h2>
              <span className="muted text-small">
                {data.settings.automation?.enabled
                  ? `每 ${data.settings.automation?.interval_minutes || 60} 分钟更新`
                  : "自动更新已关闭"}
              </span>
            </div>
            {!sources.length ? (
              <div className="panel">
                <Empty
                  title={
                    mode === "demo" ? "示例资料已经准备好" : "还没有订阅来源"
                  }
                >
                  {mode === "demo"
                    ? "下方可查看资料来源。"
                    : "添加新闻订阅或文章链接。"}
                </Empty>
              </div>
            ) : (
              <div className="source-grid">
                {sources.map((source) => (
                  <article className="panel source-card" key={source.id}>
                    <div className="source-heading">
                      <span className="source-icon">
                        {source.kind === "rss" ? (
                          <Rss size={22} />
                        ) : (
                          <FileText size={22} />
                        )}
                      </span>
                      <div>
                        <h3>{sourceLabel({ source_name: source.name, url: source.url })}</h3>
                        <span className="meta">
                          {source.kind === "rss" ? "新闻订阅" : "文章网页"} ·{" "}
                          {source.enabled ? "已启用" : "已停用"}
                        </span>
                      </div>
                      <button
                        className={`switch ${source.enabled ? "on" : ""}`}
                        role="switch"
                        aria-checked={source.enabled}
                        aria-label={`${source.enabled ? "停用" : "启用"} ${sourceLabel({ source_name: source.name, url: source.url })}`}
                        disabled={!!busy}
                        onClick={() =>
                          act(source.id, async () => {
                            await patch(`/sources/${source.id}`, {
                              enabled: !source.enabled,
                            });
                            await reload();
                          })
                        }
                      >
                        <span />
                      </button>
                    </div>
                    {safeUrl(source.url) && (
                      <a
                        className="source-url"
                        href={safeUrl(source.url)!}
                        target="_blank"
                        rel="noopener noreferrer"
                      >
                        查看来源
                        <ArrowUpRight size={13} />
                      </a>
                    )}
                    <div className="source-times">
                      <span>
                        <Clock3 size={13} />
                        最近检查{" "}
                        {source.last_checked
                          ? dateTime(source.last_checked)
                          : "尚未更新"}
                      </span>
                      {source.last_success && (
                        <span>
                          <CheckCircle2 size={13} />
                          上次成功 {dateTime(source.last_success)}
                          {source.last_added !== undefined
                            ? ` · 新增 ${source.last_added} 份`
                            : ""}
                        </span>
                      )}
                    </div>
                    {source.last_error && (
                      <ErrorNotice>上次更新未完成，请稍后重试或检查来源地址。</ErrorNotice>
                    )}
                    <div className="source-actions">
                      <button
                        className="button compact"
                        disabled={!!busy}
                        onClick={() => refreshSource(source.id)}
                      >
                        {busy === source.id ? (
                          <LoaderCircle size={15} className="spin" />
                        ) : (
                          <RefreshCw size={15} />
                        )}
                        立即更新
                      </button>
                      <button
                        className="icon-button danger"
                        aria-label={`删除来源 ${sourceLabel({ source_name: source.name, url: source.url })}`}
                        title="仅删除来源，已导入资料保留"
                        disabled={!!busy}
                        onClick={() =>
                          act(source.id, async () => {
                            await api(`/sources/${source.id}`, {
                              method: "DELETE",
                            });
                            await reload();
                            notice("来源已删除，已保存的资料仍然保留。");
                          })
                        }
                      >
                        <Trash2 size={17} />
                      </button>
                    </div>
                  </article>
                ))}
              </div>
            )}
          </>
        ) : mode === "live" ? (
          <ImportForm
            key={`${mode}-${teamId}`}
            teamId={teamId}
            events={data.events.filter((x) => x.team_id === teamId)}
            matches={data.matches.filter((x) => x.team_id === teamId)}
            reload={reload}
            notice={notice}
            openEvidence={openEvidence}
          />
        ) : (
          <div className="panel">
            <Empty
              title="把你找到的内容带进看台"
              action={
                <button className="button primary" onClick={switchLive}>
                  切换到真实资料 <ArrowRight size={16} />
                </button>
              }
            >
              切换到真实资料后即可导入。
            </Empty>
          </div>
        )}
      </details>
      <div className="section-heading">
        <h2>
          资料库 <span className="count">{documents.length}</span>
        </h2>
      </div>
      {data.settings.spoiler_free &&
        (data.spoiler_hidden?.evidence || 0) > 0 && (
          <div className="warning-notice">
            <EyeOff size={16} />
            <span>
              已隐藏 {data.spoiler_hidden?.evidence} 份可能透露赛果的资料。
            </span>
          </div>
        )}
      <div className="panel document-list">
        {documents.length ? (
          documents.map((item) => {
            const url = safeUrl(item.url);
            const content = <>
              <span className="document-icon">
                <FileText size={19} />
              </span>
              <span>
                <strong>{evidenceTitle(item)}</strong>
                <small>
                  {sourceLabel(item)} ·{" "}
                  {item.published_at
                    ? dateTime(item.published_at)
                    : "发布时间未知"}
                  {item.mode === "demo" ? " · 虚构示例" : ""}
                </small>
              </span>
              <ArrowUpRight size={17} />
            </>;
            return url ? (
              <a className="document-row" key={item.id} href={url} target="_blank" rel="noopener noreferrer">
                {content}
              </a>
            ) : (
              <button className="document-row" key={item.id} onClick={() => openEvidence(item)}>
                {content}
              </button>
            );
          })
        ) : (
          <Empty title="还没有保存的资料">
            查找消息或导入资料后，会自动保存在这里。
          </Empty>
        )}
      </div>
    </>
  );
}

function ImportForm({
  teamId,
  events,
  matches,
  reload,
  notice,
  openEvidence,
}: {
  teamId: string;
  events: ClubEvent[];
  matches: Match[];
  reload: () => Promise<void>;
  notice: Notice;
  openEvidence: (e: Evidence) => void;
}) {
  const [title, setTitle] = useState("");
  const [text, setText] = useState("");
  const [url, setUrl] = useState("");
  const [source, setSource] = useState("");
  const [published, setPublished] = useState("");
  const [kind, setKind] = useState("team");
  const [stage, setStage] = useState("");
  const [eventId, setEventId] = useState("");
  const [matchId, setMatchId] = useState("");
  const [spoiler, setSpoiler] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [saved, setSaved] = useState<Evidence | null>(null);
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (busy) return;
    setBusy(true);
    setError("");
    setSaved(null);
    try {
      const result = await post<{
        evidence: Evidence;
        event: ClubEvent;
        duplicate: boolean;
      }>("/import", {
        team_id: teamId,
        title: title.trim(),
        text: text.trim(),
        url: url.trim() || undefined,
        source_name: source.trim() || undefined,
        published_at: published ? new Date(published).toISOString() : undefined,
        kind,
        stage: stage.trim() || undefined,
        spoiler,
        event_id: eventId || undefined,
      });
      setSaved(result.evidence);
      if (matchId) {
        const match = matches.find((x) => x.id === matchId);
        try {
          await patch(`/matches/${matchId}`, {
            evidence_ids: [
              ...new Set([...(match?.evidence_ids || []), result.evidence.id]),
            ],
          });
        } catch (e) {
          setError(`资料已保存，但关联比赛未完成：${errorText(e)}`);
        }
      }
      await reload();
      notice(
        result.duplicate
          ? "这份正文已存在，未重复创建资料。"
          : "资料已保存，可向助手提问。",
      );
      setTitle("");
      setText("");
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <form className="panel form-panel" onSubmit={submit}>
      <div className="section-heading top">
        <h2>导入一份资料</h2>
        <span className="tag">真实资料</span>
      </div>
      <ErrorNotice>{error}</ErrorNotice>
      {saved && (
        <div className="success-notice">
          <CheckCircle2 size={17} />
          <span>
            {saved.spoiler_hidden
              ? saved.redaction_note || "资料已保存，内容因避剧透设置隐藏。"
              : "资料已保存"}
          </span>
          {!saved.spoiler_hidden && (
            <button
              className="text-button"
              type="button"
              onClick={() => openEvidence(saved)}
            >
              查看来源 <ArrowUpRight size={14} />
            </button>
          )}
        </div>
      )}
      <div className="form-grid">
        <label className="full">
          资料标题
          <input
            required
            minLength={2}
            maxLength={200}
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            placeholder="这份资料主要讲什么？"
          />
        </label>
        <label className="full">
          正文
          <textarea
            required
            minLength={30}
            maxLength={30000}
            rows={8}
            value={text}
            onChange={(e) => setText(e.target.value)}
            placeholder="粘贴至少 30 字的原始资料，例如训练简报、发布会记录或赛后报道。"
          />
          <span className="field-hint align-right">
            {text.length.toLocaleString()} / 30,000 字
          </span>
        </label>
        <label>
          来源名称（可选）
          <input
            value={source}
            onChange={(e) => setSource(e.target.value)}
            placeholder="例如：俱乐部发布会"
          />
        </label>
        <label>
          原文地址（可选）
          <input
            type="url"
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            placeholder="https://…"
          />
        </label>
        <label>
          发布时间（可选）
          <input
            type="datetime-local"
            value={published}
            onChange={(e) => setPublished(e.target.value)}
          />
        </label>
        <label>
          资料主题
          <select value={kind} onChange={(e) => setKind(e.target.value)}>
            <option value="team">球队动态</option>
            <option value="injury">人员伤病</option>
            <option value="transfer">转会消息</option>
            <option value="match">比赛资料</option>
          </select>
        </label>
        <label>
          进展状态（可选）
          <input
            value={stage}
            onChange={(e) => setStage(e.target.value)}
            placeholder="例如：出战成疑、待确认"
          />
        </label>
        <label>
          追加到已有事件
          <select value={eventId} onChange={(e) => setEventId(e.target.value)}>
            <option value="">建立新事件</option>
            {events.map((x) => (
              <option key={x.id} value={x.id}>
                {x.title}
              </option>
            ))}
          </select>
        </label>
        <label className="full">
          关联比赛（可选）
          <select value={matchId} onChange={(e) => setMatchId(e.target.value)}>
            <option value="">暂不关联比赛</option>
            {matches.map((x) => (
              <option value={x.id} key={x.id}>
                对阵 {x.opponent} · {dateTime(x.kickoff)}
              </option>
            ))}
          </select>
        </label>
      </div>
      <label className="check-row">
        <input
          type="checkbox"
          checked={spoiler}
          onChange={(e) => setSpoiler(e.target.checked)}
        />
        <span>
          内容包含比分、赛果或赛后信息
          <small>开启避剧透时，这份资料会默认隐藏。</small>
        </span>
      </label>
      <div className="form-actions">
        <button className="button primary" disabled={busy}>
          {busy ? (
            <Spinner text="导入中" />
          ) : (
            <>
              <Plus size={16} />
              导入资料
            </>
          )}
        </button>
      </div>
    </form>
  );
}

export function SettingsPage({
  data,
  reload,
  notice,
}: {
  data: AppState;
  reload: () => Promise<void>;
  notice: Notice;
}) {
  const [followed, setFollowed] = useState(data.settings.followed_teams || []);
  const [spoiler, setSpoiler] = useState(data.settings.spoiler_free);
  const [depth, setDepth] = useState(
    data.settings.explanation_depth || "standard",
  );
  const [baseUrl, setBaseUrl] = useState(data.settings.llm.base_url || "");
  const [model, setModel] = useState(data.settings.llm.model || "");
  const [key, setKey] = useState("");
  const [keySet, setKeySet] = useState(data.settings.llm.api_key_set);
  const [rounds, setRounds] = useState(data.settings.llm.max_rounds || 6);
  const [timeout, setTimeoutValue] = useState(
    data.settings.llm.timeout_seconds || 60,
  );
  const [temperature, setTemperature] = useState(
    data.settings.llm.temperature ?? 0.3,
  );
  const [protocol, setProtocol] = useState(
    data.settings.llm.protocol || "chat_completions",
  );
  const [sendTemperature, setSendTemperature] = useState(
    data.settings.llm.send_temperature ?? false,
  );
  const [maxTokens, setMaxTokens] = useState(
    data.settings.llm.max_output_tokens || 3000,
  );
  const [envProxy, setEnvProxy] = useState(
    data.settings.llm.use_env_proxy ?? true,
  );
  const [thinking, setThinking] = useState(
    data.settings.llm.thinking_enabled ?? false,
  );
  const modelInput = {
    base_url: baseUrl.trim(),
    model: model.trim(),
    protocol,
    send_temperature: sendTemperature,
    max_output_tokens: maxTokens,
    use_env_proxy: envProxy,
    thinking_enabled: thinking,
    max_rounds: rounds,
    timeout_seconds: timeout,
    temperature,
  };
  const modelFingerprint = JSON.stringify(modelInput);
  const [savedFingerprint, setSavedFingerprint] = useState(modelFingerprint);
  const dirty = !!key || modelFingerprint !== savedFingerprint;
  const alive = useRef(true);
  const actionLock = useRef(false);
  const [customTeam, setCustomTeam] = useState("");
  const [customEnglish, setCustomEnglish] = useState("");
  const [teamAdding, setTeamAdding] = useState(false);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [test, setTest] = useState<(ModelTest & { at: string }) | null>(null);
  useEffect(() => {
    setTest(null);
  }, [modelFingerprint, key]);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);
  async function act(type: string, work: () => Promise<void>) {
    if (actionLock.current) return;
    actionLock.current = true;
    setBusy(type);
    setError("");
    try {
      await work();
    } catch (e) {
      if (alive.current) setError(errorText(e));
    } finally {
      actionLock.current = false;
      if (alive.current) setBusy("");
    }
  }
  async function savePreferences(event: FormEvent) {
    event.preventDefault();
    if (!followed.length) {
      setError("至少关注一支球队。");
      return;
    }
    await act("preferences", async () => {
      await put<Settings>("/settings", {
        followed_teams: followed,
        spoiler_free: spoiler,
        explanation_depth: depth,
      });
      await reload();
      notice("跟队偏好已保存。");
    });
  }
  async function saveModel(event: FormEvent) {
    event.preventDefault();
    await act("model", async () => {
      const settings = await put<Settings>("/settings", {
        llm: { ...modelInput, api_key: key || undefined },
      });
      if (!alive.current) return;
      setKey("");
      setKeySet(settings.llm.api_key_set);
      setSavedFingerprint(modelFingerprint);
      await reload();
      if (alive.current)
        notice("助手连接已保存。");
    });
  }
  async function testModel() {
    await act("test", async () => {
      const result = await post<ModelTest>("/settings/test", {
        ...modelInput,
        api_key: key || undefined,
      });
      if (alive.current) setTest({ ...result, at: new Date().toISOString() });
    });
  }
  async function addTeam(event: FormEvent) {
    event.preventDefault();
    await act("team", async () => {
      const team = await post<Team>("/teams", {
        name: customTeam.trim(),
        english_name: customEnglish.trim() || undefined,
      });
      const next = [...new Set([...followed, team.id])];
      await put("/settings", { followed_teams: next });
      setFollowed(next);
      setCustomTeam("");
      setCustomEnglish("");
      setTeamAdding(false);
      await reload();
      notice("球队已添加并关注。");
    });
  }
  return (
    <>
      <div className="page-title">
        <div>
          <h1>设置</h1>
        </div>
      </div>
      <ErrorNotice>{error}</ErrorNotice>
      <nav className="settings-jump" aria-label="设置分区">
        <a href="#model-config">
          <Server size={14} />
          助手连接
        </a>
        <a href="#reading-preferences">
          <EyeOff size={14} />
          跟队习惯
        </a>
        <a href="#automation-settings">
          <RefreshCw size={14} />
          自动更新
        </a>
      </nav>
      <div className="settings-layout">
        <div>
          <form
            id="reading-preferences"
            className="panel form-panel"
            onSubmit={savePreferences}
          >
            <div className="panel-heading">
              <span className="setting-icon">
                <EyeOff size={20} />
              </span>
              <div>
                <h2>你的跟队习惯</h2>
              </div>
            </div>
            <label>关注球队</label>
            <div className="team-options">
              {data.teams.map((team) => (
                <label
                  className={`team-option ${followed.includes(team.id) ? "selected" : ""}`}
                  key={team.id}
                >
                  <input
                    type="checkbox"
                    checked={followed.includes(team.id)}
                    onChange={(e) =>
                      setFollowed(
                        e.target.checked
                          ? [...followed, team.id]
                          : followed.filter((x) => x !== team.id),
                      )
                    }
                  />
                  <Crest team={team} small />
                  <span>{team.name}</span>
                  {followed.includes(team.id) && <Check size={14} />}
                </label>
              ))}
            </div>
            <button
              className="text-button"
              type="button"
              onClick={() => setTeamAdding(!teamAdding)}
            >
              <Plus size={15} />
              {teamAdding ? "收起自定义球队" : "添加其他球队"}
            </button>
            <div className="setting-divider" />
            <label className="toggle-row">
              <span>
                <strong>避剧透</strong>
                <small>默认隐藏赛果与赛后资料，主动复盘时再揭示。</small>
              </span>
              <button
                type="button"
                className={`switch ${spoiler ? "on" : ""}`}
                role="switch"
                aria-label="避剧透"
                aria-checked={spoiler}
                onClick={() => setSpoiler(!spoiler)}
              >
                <span />
              </button>
            </label>
            <label className="spaced">
              回答详略
              <select
                value={depth}
                onChange={(e) => setDepth(e.target.value as typeof depth)}
              >
                <option value="brief">简洁</option>
                <option value="standard">适中</option>
                <option value="deep">详细</option>
              </select>
            </label>
            <div className="setting-divider" />
            <div className="form-actions">
              <button className="button primary" disabled={!!busy}>
                {busy === "preferences" ? (
                  <Spinner text="保存中" />
                ) : (
                  <>
                    <Save size={15} />
                    保存跟队偏好
                  </>
                )}
              </button>
            </div>
          </form>
          {teamAdding && (
            <form className="panel form-panel" onSubmit={addTeam}>
              <h2>添加球队</h2>
              <label>
                球队名称
                <input
                  required
                  maxLength={80}
                  value={customTeam}
                  onChange={(e) => setCustomTeam(e.target.value)}
                  placeholder="输入球队中文名称"
                />
              </label>
              <label className="spaced">
                英文名称（可选）
                <input
                  maxLength={100}
                  value={customEnglish}
                  onChange={(e) => setCustomEnglish(e.target.value)}
                />
              </label>
              <div className="form-actions">
                <button className="button primary" disabled={!!busy}>
                  {busy === "team" ? <Spinner text="添加中" /> : "添加并关注"}
                </button>
              </div>
            </form>
          )}
          <AutomationSettings data={data} reload={reload} notice={notice} />
        </div>
        <form
          id="model-config"
          className="panel form-panel model-panel"
          onSubmit={saveModel}
        >
          <fieldset disabled={!!busy}>
            <div className="panel-heading">
              <span className="setting-icon">
                <Server size={20} />
              </span>
              <div>
                <h2>助手连接</h2>
              </div>
            </div>
            <div className={`saved-indicator ${dirty ? "dirty" : ""}`}>
              {dirty ? <CircleAlert size={14} /> : <CheckCircle2 size={14} />}{" "}
              {dirty
                ? "有尚未保存的修改"
                : model
                  ? "连接设置已保存"
                  : "尚未连接"}
            </div>
            <div className="connection-note">
              <KeyRound size={18} />
              <span>
                更换服务地址时，请重新填写密钥。测试成功后记得保存。
              </span>
            </div>
            <div className="form-grid">
              <label className="full">
                接口协议
                <select
                  value={protocol}
                  onChange={(e) =>
                    setProtocol(e.target.value as typeof protocol)
                  }
                >
                  <option value="chat_completions">Chat Completions</option>
                  <option value="responses">Responses</option>
                </select>
              </label>
              <label className="full">
                服务地址
                <input
                  type="url"
                  value={baseUrl}
                  onChange={(e) => {
                    setBaseUrl(e.target.value);
                    setTest(null);
                  }}
                  placeholder="https://your-provider.example/v1"
                />
                <span className="field-hint">
                  填写兼容接口的基础地址，通常以 /v1 结尾。
                </span>
              </label>
              <label className="full">
                模型名称
                <input
                  value={model}
                  maxLength={200}
                  onChange={(e) => {
                    setModel(e.target.value);
                    setTest(null);
                  }}
                  placeholder="服务商提供的模型 ID"
                />
              </label>
              <label className="full">
                API 密钥
                <span className="key-status">
                  {keySet ? "已保存密钥" : "尚未保存密钥"}
                </span>
                <input
                  type="password"
                  value={key}
                  autoComplete="new-password"
                  onChange={(e) => {
                    setKey(e.target.value);
                    setTest(null);
                  }}
                  placeholder={
                    keySet
                      ? "留空保留，输入新密钥可替换"
                      : "输入密钥（本地服务可留空）"
                  }
                />
              </label>
            </div>
            {keySet && (
              <button
                type="button"
                className="text-button danger"
                disabled={!!busy}
                onClick={() =>
                  act("clear", async () => {
                    const result = await put<Settings>("/settings", {
                      llm: { clear_api_key: true },
                    });
                    setKeySet(result.llm.api_key_set);
                    setKey("");
                    setTest(null);
                    await reload();
                    notice("已清除保存的密钥。");
                  })
                }
              >
                <Trash2 size={14} />
                清除已保存的密钥
              </button>
            )}
            <details className="advanced-settings">
              <summary>高级设置</summary>
              <div className="form-grid">
                <label>
                  最多查询轮数
                  <input
                    type="number"
                    min={2}
                    max={16}
                    required
                    value={rounds}
                    onChange={(e) => setRounds(Number(e.target.value))}
                  />
                </label>
                <label>
                  单次请求超时（秒）
                  <input
                    type="number"
                    min={10}
                    max={120}
                    required
                    value={timeout}
                    onChange={(e) => setTimeoutValue(Number(e.target.value))}
                  />
                </label>
                <label className="full">
                  输出长度上限（词元）
                  <input
                    type="number"
                    min={256}
                    max={16000}
                    required
                    value={maxTokens}
                    onChange={(e) => setMaxTokens(Number(e.target.value))}
                  />
                </label>
                <label className="inline-check full">
                  <input
                    type="checkbox"
                    checked={envProxy}
                    onChange={(e) => setEnvProxy(e.target.checked)}
                  />
                  使用系统代理
                </label>
                <label className="inline-check full">
                  <input
                    type="checkbox"
                    checked={sendTemperature}
                    onChange={(e) => setSendTemperature(e.target.checked)}
                  />
                  自定义回答随机度
                </label>
                {sendTemperature && (
                  <label className="full">
                    随机度 · {temperature.toFixed(1)}
                    <input
                      className="range"
                      type="range"
                      min={0}
                      max={1}
                      step={0.1}
                      value={temperature}
                      onChange={(e) => setTemperature(Number(e.target.value))}
                    />
                  </label>
                )}
                {model.toLowerCase().includes("deepseek") && (
                  <label className="inline-check full">
                    <input
                      type="checkbox"
                      checked={thinking}
                      onChange={(e) => setThinking(e.target.checked)}
                    />
                    启用 DeepSeek 深度思考
                  </label>
                )}
              </div>
            </details>
            <div className="form-actions split">
              <button
                type="button"
                className="button"
                disabled={!!busy}
                onClick={testModel}
              >
                {busy === "test" ? (
                  <Spinner text="连接测试中" />
                ) : (
                  <>
                    <Wifi size={16} />
                    测试当前连接
                  </>
                )}
              </button>
              <button className="button primary" disabled={!!busy}>
                {busy === "model" ? (
                  <Spinner text="保存中" />
                ) : (
                  <>
                    <Save size={15} />
                    保存连接
                  </>
                )}
              </button>
            </div>
            {test && (
              <div
                className={
                  test.ok
                    ? test.tool_support === false
                      ? "test-result warning"
                      : "test-result success"
                    : "test-result failure"
                }
                role="status"
              >
                {test.ok && test.tool_support !== false ? (
                  <CheckCircle2 size={19} />
                ) : (
                  <CircleAlert size={19} />
                )}
                <div>
                  <strong>
                    {test.ok
                      ? test.tool_support === false
                        ? "服务已连接，查找资料功能暂不可用"
                        : "连接测试成功"
                      : "连接测试失败"}
                  </strong>
                  {!test.ok && (
                    <>
                      <p>请检查服务地址、模型名称和密钥。</p>
                      <details>
                        <summary>查看连接问题</summary>
                        <p>{test.message}</p>
                      </details>
                    </>
                  )}
                  <small>{dateTime(test.at)}</small>
                </div>
              </div>
            )}
          </fieldset>
        </form>
      </div>
    </>
  );
}

export function MatchForm({
  teamId,
  match,
  evidence,
  onSaved,
  notice,
}: {
  teamId: string;
  match?: Match;
  evidence: Evidence[];
  onSaved: () => Promise<void>;
  notice: Notice;
}) {
  const [opponent, setOpponent] = useState(match?.opponent || "");
  const [competition, setCompetition] = useState(match?.competition || "");
  const [kickoff, setKickoff] = useState("");
  const [venue, setVenue] = useState(match?.venue || "home");
  const [status, setStatus] = useState(match?.status || "scheduled");
  const [score, setScore] = useState(match?.score || "");
  const [note, setNote] = useState(match?.note || "");
  const [ids, setIds] = useState(match?.evidence_ids || []);
  const [scoreDirty, setScoreDirty] = useState(false);
  const [noteDirty, setNoteDirty] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (busy) return;
    setBusy(true);
    setError("");
    try {
      if (match)
        await patch(`/matches/${match.id}`, {
          status,
          ...(!match.spoiler_hidden || scoreDirty
            ? { score: status === "finished" ? score.trim() || null : null }
            : {}),
          ...(!match.spoiler_hidden || noteDirty ? { note: note.trim() } : {}),
          ...(!match.spoiler_hidden ? { evidence_ids: ids } : {}),
        });
      else
        await post("/matches", {
          team_id: teamId,
          opponent: opponent.trim(),
          competition: competition.trim(),
          kickoff: new Date(kickoff).toISOString(),
          venue,
          status,
          score: status === "finished" ? score.trim() || null : null,
          note: note.trim(),
          evidence_ids: ids,
        });
      await onSaved();
      notice(
        match ? "比赛资料已更新。" : "比赛已添加，可在资料导入时关联证据。",
      );
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <form onSubmit={submit} className="match-form">
      <ErrorNotice>{error}</ErrorNotice>
      {match?.spoiler_hidden && (
        <div className="warning-notice">
          <EyeOff size={16} />
          <span>
            赛果受避剧透保护。未修改的比分、备注和关联资料会原样保留；填写新内容会更新对应字段。
          </span>
        </div>
      )}
      <div className="form-grid">
        {!match && (
          <>
            <label className="full">
              对手名称
              <input
                required
                maxLength={100}
                value={opponent}
                onChange={(e) => setOpponent(e.target.value)}
                placeholder="输入对手球队名称"
              />
            </label>
            <label>
              赛事名称
              <input
                required
                maxLength={100}
                value={competition}
                onChange={(e) => setCompetition(e.target.value)}
                placeholder="例如：联赛"
              />
            </label>
            <label>
              主客场
              <select value={venue} onChange={(e) => setVenue(e.target.value)}>
                <option value="home">主场</option>
                <option value="away">客场</option>
                <option value="neutral">中立场地</option>
              </select>
            </label>
            <label className="full">
              开赛时间（本机时区）
              <input
                type="datetime-local"
                required
                value={kickoff}
                onChange={(e) => setKickoff(e.target.value)}
              />
            </label>
          </>
        )}
        <label>
          比赛状态
          <select value={status} disabled={!!match?.source_match_id} onChange={(e) => setStatus(e.target.value)}>
            <option value="scheduled">尚未结束</option>
            <option value="finished">已结束</option>
          </select>
        </label>
        {status === "finished" && (
          <label>
            比分（可选）
            <input
              value={score}
              readOnly={!!match?.source_match_id}
              onChange={(e) => {
                setScore(e.target.value);
                setScoreDirty(true);
              }}
              maxLength={30}
              placeholder="例如：2–1"
            />
          </label>
        )}
        {match?.source_match_id && <p className="field-hint full">官方赛果与状态通过同步更新，备注和关联资料仍可编辑。</p>}
        <label className="full">
          比赛备注（可选）
          <textarea
            rows={3}
            value={note}
            onChange={(e) => {
              setNote(e.target.value);
              setNoteDirty(true);
            }}
            maxLength={2000}
            placeholder="只记录有依据的比赛信息"
          />
        </label>
      </div>
      <div className="spaced">
        <label>关联原始资料</label>
        {match?.spoiler_hidden ? (
          <p className="fine-print">
            关联资料受避剧透保护并保持不变。需要编辑时，请先在设置中关闭避剧透。
          </p>
        ) : evidence.length ? (
          <div className="evidence-select">
            {evidence.map((item) => (
              <label key={item.id} className="check-row">
                <input
                  type="checkbox"
                  checked={ids.includes(item.id)}
                  onChange={(e) =>
                    setIds(
                      e.target.checked
                        ? [...ids, item.id]
                        : ids.filter((x) => x !== item.id),
                    )
                  }
                />
                <span>{evidenceTitle(item)}</span>
              </label>
            ))}
          </div>
        ) : (
          <p className="fine-print">
            当前没有可关联资料。保存比赛后，可前往资料来源导入。
          </p>
        )}
      </div>
      <div className="form-actions">
        <button className="button primary" disabled={busy}>
          {busy ? (
            <Spinner text="保存中" />
          ) : (
            <>
              <Save size={16} />
              {match ? "保存比赛资料" : "添加比赛"}
            </>
          )}
        </button>
      </div>
    </form>
  );
}
