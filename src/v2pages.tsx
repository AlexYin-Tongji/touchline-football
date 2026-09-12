import { useEffect, useRef, useState, type FormEvent } from "react";
import {
  ArrowRight,
  ArrowUpRight,
  Bell,
  Check,
  Pause,
  Play,
  Plus,
  RefreshCw,
  Search,
  Trash2,
} from "lucide-react";
import { api, errorText, patch, post } from "./api";
import { dateTime, Empty, ErrorNotice, evidenceTitle, safeUrl, sourceLabel, Spinner } from "./components";
import type {
  AppState,
  Candidate,
  CatalogSource,
  Evidence,
  Mode,
  Run,
  Tracking,
} from "./types";

type Notice = (message: string, error?: boolean) => void;
type TrackingSeed = {
  title: string;
  query: string;
  event_id?: string;
  match_id?: string;
};
const trackingStatus: Record<string, string> = {
  idle: "等待首次检查",
  running: "正在核实进展",
  changed: "发现新进展",
  unchanged: "暂无明确变化",
  failed: "检查未完成",
  paused: "已暂停",
};

export function TrackingPage({
  data,
  teamId,
  mode,
  seed,
  clearSeed,
  reload,
  notice,
  switchLive,
  openRun,
}: {
  data: AppState;
  teamId: string;
  mode: Mode;
  seed: TrackingSeed | null;
  clearSeed: () => void;
  reload: () => Promise<void>;
  notice: Notice;
  switchLive: () => void;
  openRun: (run: Run) => void;
}) {
  const [adding, setAdding] = useState(!!seed);
  const [title, setTitle] = useState(seed?.title || "");
  const [query, setQuery] = useState(seed?.query || "");
  const [eventId, setEventId] = useState(seed?.event_id || "");
  const [matchId, setMatchId] = useState(seed?.match_id || "");
  const [interval, setIntervalValue] = useState(120);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [editing, setEditing] = useState<string | null>(null);
  const alive = useRef(true);
  const locked = useRef(false);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);
  const items = (data.tracking || []).filter(
    (x) => x.team_id === teamId && x.mode === mode,
  );
  useEffect(() => {
    if (!items.some((x) => x.status === "running")) return;
    const timer = window.setInterval(
      () => reload().catch(() => undefined),
      2500,
    );
    return () => window.clearInterval(timer);
  }, [items.some((x) => x.status === "running"), reload]);
  async function act(key: string, work: () => Promise<void>) {
    if (locked.current) return;
    locked.current = true;
    setBusy(key);
    setError("");
    try {
      await work();
    } catch (e) {
      if (alive.current) setError(errorText(e));
    } finally {
      locked.current = false;
      if (alive.current) setBusy("");
    }
  }
  function resetForm() {
    setAdding(false);
    setTitle("");
    setQuery("");
    setEventId("");
    setMatchId("");
    setEditing(null);
    clearSeed();
  }
  async function save(event: FormEvent) {
    event.preventDefault();
    await act("save", async () => {
      if (editing)
        await patch(`/tracking/${editing}`, {
          title: title.trim(),
          query: query.trim(),
          interval_minutes: interval,
        });
      else
        await post<Tracking>("/tracking", {
          team_id: teamId,
          title: title.trim(),
          query: query.trim(),
          interval_minutes: interval,
          event_id: eventId || undefined,
          match_id: matchId || undefined,
        });
      if (!alive.current) return;
      await reload();
      if (!alive.current) return;
      resetForm();
      notice(
        editing ? "关注主题已更新。" : "已开始关注这个主题。",
      );
    });
  }
  async function viewRun(id: string) {
    await act(id, async () => {
      const run = await api<Run>(`/runs/${id}`);
      if (alive.current && run.team_id === teamId && run.mode === mode)
        openRun(run);
    });
  }
  return (
    <>
      <div className="page-title">
        <div>
          <h1>关注主题</h1>
        </div>
        {mode === "live" && (
          <button
            className="button primary"
            onClick={() => {
              resetForm();
              setAdding(true);
            }}
          >
            <Plus size={16} />
            添加主题
          </button>
        )}
      </div>
      {mode === "demo" ? (
        <div className="mode-callout">
          <Bell size={22} />
          <div>
            <strong>切换到真实资料后，即可持续关注消息进展。</strong>
          </div>
          <button className="button" onClick={switchLive}>
            开始真实跟队
            <ArrowRight size={15} />
          </button>
        </div>
      ) : (
        <>
          <ErrorNotice>{error}</ErrorNotice>
          {adding && (
            <form className="panel form-panel tracking-form" onSubmit={save}>
              <h2>{editing ? "编辑关注主题" : "你希望持续关注什么？"}</h2>
              <div className="form-grid">
                <label className="full">
                  主题名称
                  <input
                    required
                    maxLength={120}
                    value={title}
                    onChange={(e) => setTitle(e.target.value)}
                    placeholder="例如：主力后腰的伤情进展"
                  />
                </label>
                <label className="full">
                  具体关注点
                  <textarea
                    required
                    maxLength={4000}
                    rows={3}
                    value={query}
                    onChange={(e) => setQuery(e.target.value)}
                    placeholder="追踪训练、名单与官方消息，判断是否影响下一场出场。"
                  />
                </label>
                <label>
                  检查间隔
                  <select
                    value={interval}
                    onChange={(e) => setIntervalValue(Number(e.target.value))}
                  >
                    <option value={30}>每 30 分钟</option>
                    <option value={60}>每小时</option>
                    <option value={120}>每 2 小时</option>
                    <option value={360}>每 6 小时</option>
                    <option value={1440}>每天</option>
                  </select>
                </label>
                {!editing && (
                  <label>
                    关联比赛（可选）
                    <select
                      value={matchId}
                      onChange={(e) => setMatchId(e.target.value)}
                    >
                      <option value="">持续关注球队</option>
                      {data.matches
                        .filter((m) => m.team_id === teamId)
                        .map((m) => (
                          <option value={m.id} key={m.id}>
                            对阵{m.opponent} · {dateTime(m.kickoff)}
                          </option>
                        ))}
                    </select>
                  </label>
                )}
              </div>
              <p className="tracking-note">
                应用运行时自动检查，可随时暂停。
              </p>
              <div className="form-actions">
                <button type="button" className="button" onClick={resetForm}>
                  取消
                </button>
                <button className="button primary" disabled={!!busy}>
                  {busy === "save" ? (
                    <Spinner text="保存中" />
                  ) : (
                    <>
                      <Bell size={15} />
                      保存主题
                    </>
                  )}
                </button>
              </div>
            </form>
          )}
          {!items.length && !adding ? (
            <Empty
              title="从一个具体的关注点开始"
              action={
                <button className="button" onClick={() => setAdding(true)}>
                  <Plus size={15} />
                  添加关注主题
                </button>
              }
            >
              例如球员伤情、转会消息或首发安排。
            </Empty>
          ) : (
            <div className="tracking-list">
              {items.map((item) => (
                <article className="tracking-card" key={item.id}>
                  <div className="tracking-top">
                    <div>
                      <span className={`tracking-status ${item.status}`}>
                        <span className="mode-dot" />
                        {trackingStatus[item.enabled ? item.status : "paused"]}
                      </span>
                      <h2>{item.title}</h2>
                    </div>
                    <button
                      className="icon-button danger"
                      title="移除关注主题"
                      aria-label={`删除跟进 ${item.title}`}
                      disabled={!!busy}
                      onClick={() =>
                        act(item.id, async () => {
                          await api(`/tracking/${item.id}`, {
                            method: "DELETE",
                          });
                          if (alive.current) await reload();
                        })
                      }
                    >
                      <Trash2 size={16} />
                    </button>
                  </div>
                  <p className="tracking-query">{item.query}</p>
                  {item.summary && (
                    <p className="tracking-summary">{item.summary}</p>
                  )}
                  {item.last_error && (
                    <ErrorNotice>本次检查未完成，可以稍后重试。</ErrorNotice>
                  )}
                  <div className="tracking-meta">
                    <span>
                      上次检查：
                      {item.last_checked
                        ? dateTime(item.last_checked)
                        : "尚未检查"}
                    </span>
                    <span>
                      {item.enabled
                        ? `下次检查：${dateTime(item.next_check)}`
                        : "暂停自动检查"}
                    </span>
                  </div>
                  <div className="tracking-actions">
                    <button
                      className="button compact"
                      disabled={!!busy || item.status === "running"}
                      onClick={() =>
                        act(item.id, async () => {
                          await post<Run>(`/tracking/${item.id}/check`);
                          if (alive.current) {
                            await reload();
                            notice("正在检查最新进展。");
                          }
                        })
                      }
                    >
                      {item.status === "running" ? (
                        <Spinner text="正在检查" />
                      ) : (
                        <>
                          <RefreshCw size={14} />
                          立即检查
                        </>
                      )}
                    </button>
                    <button
                      className="text-button"
                      disabled={!!busy}
                      onClick={() =>
                        act(item.id, async () => {
                          await patch(`/tracking/${item.id}`, {
                            enabled: !item.enabled,
                          });
                          if (alive.current) await reload();
                        })
                      }
                    >
                      {item.enabled ? <Pause size={14} /> : <Play size={14} />}
                      {item.enabled ? "暂停" : "恢复跟进"}
                    </button>
                    <button
                      className="text-button"
                      onClick={() => {
                        setEditing(item.id);
                        setTitle(item.title);
                        setQuery(item.query);
                        setIntervalValue(item.interval_minutes);
                        setAdding(true);
                        window.scrollTo({ top: 0, behavior: "smooth" });
                      }}
                    >
                      编辑主题
                    </button>
                    {item.last_run_id && (
                      <button
                        className="text-button"
                        disabled={!!busy}
                        onClick={() => viewRun(item.last_run_id!)}
                      >
                        查看本次解读
                        <ArrowUpRight size={13} />
                      </button>
                    )}
                  </div>
                  {!!item.updates?.length && (
                    <details className="tracking-updates">
                      <summary>
                        进展时间线 · {item.updates.length} 次记录
                      </summary>
                      {[...item.updates].reverse().map((update, i) => (
                        <div
                          className="tracking-update"
                          key={`${update.at}-${i}`}
                        >
                          <time>{dateTime(update.at)}</time>
                          <p>{update.summary}</p>
                          <button
                            className="text-button"
                            disabled={!!busy}
                            onClick={() => viewRun(update.run_id)}
                          >
                            查看解读
                            <ArrowUpRight size={13} />
                          </button>
                        </div>
                      ))}
                    </details>
                  )}
                </article>
              ))}
            </div>
          )}
        </>
      )}
    </>
  );
}

export function ResearchPanel({
  teamId,
  reload,
  notice,
  openEvidence,
}: {
  teamId: string;
  reload: () => Promise<void>;
  notice: Notice;
  openEvidence: (e: Evidence) => void;
}) {
  const [query, setQuery] = useState("");
  const [catalog, setCatalog] = useState<CatalogSource[]>([]);
  const [candidates, setCandidates] = useState<Candidate[]>([]);
  const [readIds, setReadIds] = useState<string[]>([]);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [searched, setSearched] = useState(false);
  const alive = useRef(true);
  const locked = useRef(false);
  useEffect(() => {
    alive.current = true;
    api<{ sources: CatalogSource[] }>(
      `/source-catalog?team_id=${encodeURIComponent(teamId)}`,
    )
      .then((result) => {
        if (alive.current) setCatalog(result.sources || []);
      })
      .catch((e) => {
        if (alive.current) setError(errorText(e));
      });
    return () => {
      alive.current = false;
    };
  }, [teamId]);
  async function search(event: FormEvent) {
    event.preventDefault();
    if (locked.current) return;
    locked.current = true;
    setBusy("search");
    setError("");
    setCandidates([]);
    try {
      const result = await post<{ candidates: Candidate[]; errors: string[] }>(
        "/research",
        { team_id: teamId, query: query.trim(), limit: 8 },
      );
      if (alive.current) {
        setCandidates(result.candidates || []);
        setSearched(true);
        if (result.errors?.length) setError("部分来源暂时无法读取，可以稍后重试。");
      }
    } catch (e) {
      if (alive.current) setError(errorText(e));
    } finally {
      locked.current = false;
      if (alive.current) setBusy("");
    }
  }
  async function read(item: Candidate) {
    if (locked.current) return;
    locked.current = true;
    setBusy(item.id);
    setError("");
    try {
      const result = await post<{ evidence: Evidence; duplicate?: boolean }>(
        "/research/read",
        { team_id: teamId, candidate_id: item.id },
      );
      if (!alive.current) return;
      setReadIds((ids) => [...ids, item.id]);
      await reload();
      if (!alive.current) return;
      notice(
        result.evidence?.spoiler_hidden
          ? result.evidence.redaction_note ||
              "资料已保存，内容因避剧透设置隐藏。"
          : result.duplicate
            ? "这份资料已保存。"
            : "资料已保存。",
      );
      if (result.evidence && !result.evidence.spoiler_hidden)
        openEvidence(result.evidence);
    } catch (e) {
      if (alive.current) setError(errorText(e));
    } finally {
      locked.current = false;
      if (alive.current) setBusy("");
    }
  }
  return (
    <section className="research-panel">
      <div className="section-heading">
        <h2>查找球队消息</h2>
      </div>
      <form className="research-search" onSubmit={search}>
        <input
          aria-label="查找主题"
          maxLength={1000}
          placeholder="留空查找近期消息，或输入球员、伤情、转会…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
        <button className="button primary" disabled={!!busy}>
          {busy === "search" ? (
            <Spinner text="正在查找" />
          ) : (
            <>
              <Search size={16} />
              查找消息
            </>
          )}
        </button>
      </form>
      <div className="catalog-list">
        <span>当前来源</span>
        {catalog.map((source) =>
          safeUrl(source.url) ? (
            <a
              key={source.id}
              href={safeUrl(source.url)!}
              target="_blank"
              rel="noopener noreferrer"
            >
              {sourceLabel({ source_name: source.name, url: source.url })}
              <ArrowUpRight size={11} />
            </a>
          ) : null,
        )}
        {!catalog.length && <span>可以在下方添加球队来源</span>}
      </div>
      <ErrorNotice>{error}</ErrorNotice>
      {candidates.map((item) => (
        <div className="candidate" key={item.id}>
          <div>
            <h3>
              {safeUrl(item.url) ? (
                <a
                  href={safeUrl(item.url)!}
                  target="_blank"
                  rel="noopener noreferrer"
                >
                  {evidenceTitle(item)}
                </a>
              ) : (
                evidenceTitle(item)
              )}
            </h3>
            <p>
              {sourceLabel(item)} ·{" "}
              {item.published_at
                ? dateTime(item.published_at)
                : "发布时间未知"}
            </p>
          </div>
          <button
            className="button compact"
            disabled={!!busy || readIds.includes(item.id)}
            onClick={() => read(item)}
          >
            {busy === item.id ? (
              <Spinner text="保存中" />
            ) : readIds.includes(item.id) ? (
              <>
                <Check size={13} />
                已保存
              </>
            ) : (
              <>
                保存资料
                <ArrowRight size={13} />
              </>
            )}
          </button>
        </div>
      ))}
      {searched && !busy && !candidates.length && !error && (
        <p className="fine-print">
          当前来源中没有找到匹配文章。可以缩短关键词，或添加新的公开来源。
        </p>
      )}
    </section>
  );
}
