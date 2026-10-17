import { memo, useCallback, useEffect, useRef, useState } from "react";
import {
  ArrowRight,
  CalendarDays,
  ChevronDown,
  MessageCircle,
  Newspaper,
  Sparkles,
} from "lucide-react";
import { api, errorText, post } from "./api";
import { WorkflowControls } from "./WorkflowControls";
import { Crest, dateTime, Modal, SourceGapNotice, Spinner } from "./components";
import { findClub } from "./clubCatalog";
import { clubName } from "./clubNames";
import {
  friendlyTime,
  Markdown,
  Photo,
  Sources,
  uniqueImages,
  type MediaImage,
  type SourceLink,
} from "./v7Shared";
import type { AppState, AutoReport, SourceGap, Team } from "./types";
import type { AssistantPrompt } from "./v7Assistant";

interface FeedItem {
  id: string;
  title: string;
  summary: string;
  topic?: string;
  updated_at?: string;
  published_at?: string | null;
  published_date?: string | null;
  organized_at?: string | null;
  images?: MediaImage[];
  sources?: SourceLink[];
  report_id?: string | null;
  status?: string;
  source_gap_count?: number;
  team_id: string;
}
interface Preparation {
  kind: "news";
  team_id: string;
  status: "idle" | "running" | "ready" | "partial" | "failed" | "empty" | "interrupted";
  message: string;
  updated_at?: string;
  counts?: { candidates?: number; attempted_reads?: number; readable?: number; added?: number };
  source_errors?: SourceGap[];
}
interface FeedResponse {
  items: FeedItem[];
  next_cursor: string | null;
  preparation?: Preparation;
  refresh?: {
    status?: string;
    last_updated?: string;
    error?: string;
    pending?: number;
  };
  pending_count?: number;
  processing?: {
    queued: number;
    generating: number;
    retrying: number;
    failed: number;
    needs_configuration: number;
    budget_blocked: number;
    waiting_sources: number;
    total_pending: number;
    next_retry_at?: string | null;
    note: string;
    can_retry: boolean;
    manual_count?: number;
    can_organize?: boolean;
  };
}
type IllustratedReport = Omit<AutoReport, "report"> & {
  report?: Omit<NonNullable<AutoReport["report"]>, "sections"> & {
    images?: MediaImage[];
    sections: (NonNullable<AutoReport["report"]>["sections"][number] & {
      illustrations?: MediaImage[];
    })[];
  };
};
const topics = [
  { id: "", name: "全部动态" },
  { id: "match", name: "比赛" },
  { id: "transfer", name: "转会" },
  { id: "injury", name: "伤停" },
  { id: "team", name: "球队" },
];
const topicName = (value?: string) =>
  topics.find((item) => item.id === value)?.name || "球队动态";
const latestPreparation = (current: Preparation | null, next: Preparation) => {
  if (current?.team_id !== next.team_id) return next;
  if (next.status === "idle" && current.status !== "idle") return current;
  if (current.updated_at && next.updated_at && Date.parse(current.updated_at) > Date.parse(next.updated_at)) return current;
  return next;
};

export const FeedView = memo(function FeedView({
  team,
  data,
  ask,
  openModel,
}: {
  team: Team;
  data: AppState;
  ask: (value: AssistantPrompt) => void;
  openModel: () => void;
}) {
  const [feed, setFeed] = useState<FeedResponse | null>(null);
  const [topic, setTopic] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [preparation, setPreparation] = useState<Preparation | null>(null);
  const [preparationError, setPreparationError] = useState("");
  const [selected, setSelected] = useState<FeedItem | null>(null);
  const [report, setReport] = useState<IllustratedReport | null>(null);
  const [reading, setReading] = useState(false);
  const [readError, setReadError] = useState("");
  const [asking, setAsking] = useState<string | null>(null);
  const serial = useRef(0);
  const loadedItems = useRef(20);
  loadedItems.current = Math.max(20, feed?.items.length || 0);
  const readSerial = useRef(0);
  const activeTeam = useRef(team.id);
  const mounted = useRef(true);
  const loadingRequest = useRef(false);
  const prepareSerial = useRef(0);
  activeTeam.current = team.id;
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      readSerial.current++;
    };
  }, []);
  const load = useCallback(
    async (cursor?: string, preservePages = false) => {
      if (!mounted.current || (preservePages && loadingRequest.current)) return;
      const request = ++serial.current;
      loadingRequest.current = true;
      const requestedTeam = team.id;
      setLoading(true);
      try {
        const target = preservePages ? loadedItems.current : 20;
        let result = await api<FeedResponse>(
          `/feed?team_id=${encodeURIComponent(requestedTeam)}&limit=${Math.min(40, target)}${topic ? `&topic=${encodeURIComponent(topic)}` : ""}${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}`,
        );
        while (preservePages && result.next_cursor && result.items.length < target) {
          if (request !== serial.current || activeTeam.current !== requestedTeam) return;
          const page = await api<FeedResponse>(
            `/feed?team_id=${encodeURIComponent(requestedTeam)}&limit=${Math.min(40, target - result.items.length)}${topic ? `&topic=${encodeURIComponent(topic)}` : ""}&cursor=${encodeURIComponent(result.next_cursor)}`,
          );
          result = { ...page, items: [...result.items, ...page.items.filter(item => !result.items.some(old => old.id === item.id))] };
        }
        if (request !== serial.current || activeTeam.current !== requestedTeam)
          return;
        setFeed((previous) =>
          cursor && previous
            ? {
                ...result,
                items: [
                  ...previous.items,
                  ...result.items.filter(
                    (item) => !previous.items.some((old) => old.id === item.id),
                  ),
                ],
              }
            : result,
        );
        setError("");
        const nextPreparation = result.preparation;
        if (nextPreparation?.team_id === requestedTeam)
          setPreparation((current) => latestPreparation(current, nextPreparation));
      } catch (err) {
        if (request === serial.current) setError(errorText(err));
      } finally {
        if (request === serial.current) {
          loadingRequest.current = false;
          setLoading(false);
        }
      }
    },
    [team.id, topic],
  );
  useEffect(() => {
    setFeed(null);
    setSelected(null);
    void load();
    return () => {
      serial.current++;
    };
  }, [load]);
  useEffect(() => {
    const requestedTeam = team.id;
    const request = ++prepareSerial.current;
    setPreparation(null);
    setPreparationError("");
    void post<Preparation>(`/teams/${encodeURIComponent(requestedTeam)}/prepare`, { kind: "news" })
      .then((next) => {
        if (!mounted.current || activeTeam.current !== requestedTeam || request !== prepareSerial.current) return;
        if (next.kind === "news" && next.team_id === requestedTeam)
          setPreparation((current) => latestPreparation(current, next));
      })
      .catch((err) => {
        if (mounted.current && activeTeam.current === requestedTeam && request === prepareSerial.current)
          setPreparationError(errorText(err));
      });
    return () => { prepareSerial.current++; };
  }, [team.id]);
  useEffect(() => {
    const timer = window.setInterval(() => {
      if (document.visibilityState === "visible") void load(undefined, true);
    }, preparation?.status === "running" ? 5000 : 60000);
    return () => clearInterval(timer);
  }, [load, preparation?.status]);
  const workflowChanged = useCallback(() => {
    void load(undefined, true);
  }, [load]);
  const read = async (item: FeedItem) => {
    const request = ++readSerial.current;
    setSelected(item);
    setReport(null);
    setReadError("");
    setReading(!!item.report_id);
    if (!item.report_id) return;
    try {
      const result = await api<IllustratedReport>(
        `/reports/${encodeURIComponent(item.report_id)}`,
      );
      if (request === readSerial.current) {
        setReport(result);
        if (result.status !== "completed" || !result.report) {
          setReadError("这条消息正在更新，请稍后刷新动态。");
          setFeed((previous) =>
            previous
              ? {
                  ...previous,
                  items: previous.items.filter((entry) => entry.id !== item.id),
                }
              : previous,
          );
        }
      }
    } catch (err) {
      if (request === readSerial.current) {
        setReadError(errorText(err));
        setFeed((previous) =>
          previous
            ? {
                ...previous,
                items: previous.items.filter((entry) => entry.id !== item.id),
              }
            : previous,
        );
      }
    } finally {
      if (request === readSerial.current) setReading(false);
    }
  };
  const askAbout = async (item: FeedItem) => {
    if (asking) return;
    const requestedTeam = team.id;
    setAsking(item.id);
    try {
      const fresh = item.report_id
        ? await api<IllustratedReport>(
            `/reports/${encodeURIComponent(item.report_id)}`,
          )
        : null;
      if (!mounted.current || activeTeam.current !== requestedTeam) return;
      if (item.report_id && (fresh?.status !== "completed" || !fresh.report))
        throw new Error("这条消息的资料已变化，请刷新动态后重试。");
      ask({
        text: `请进一步解释这条关于${team.name}的消息：${fresh?.report?.title || item.title}`,
        skill: "auto",
        evidenceIds: (fresh?.report?.sources || item.sources)
          ?.flatMap((source) => (source.id ? [source.id] : []))
          .slice(0, 5),
      });
    } catch (err) {
      if (mounted.current && activeTeam.current === requestedTeam) {
        setError(errorText(err));
        setFeed((previous) =>
          previous
            ? {
                ...previous,
                items: previous.items.filter((entry) => entry.id !== item.id),
              }
            : previous,
        );
      }
    } finally {
      if (mounted.current && activeTeam.current === requestedTeam) setAsking(null);
    }
  };
  const items = (feed?.items || []).map((item) => ({
      ...item,
      // 卡片上给两张：一张常常只是个题图，两张才能说明这是"一组消息"。
      // 后端最多送四张，三张的网格会让每张都小到看不清。
      images: uniqueImages(item.images).slice(0, 2),
    }));
  const upcoming = data.matches
    .filter(
      (item) =>
        item.team_id === team.id &&
        item.status !== "finished" &&
        item.kickoff &&
        Date.parse(item.kickoff) >= Date.now(),
    )
    .sort((a, b) => (a.kickoff || "").localeCompare(b.kickoff || ""))[0];
  const seenStoryImages = new Set<string>();
  const sections = report?.report?.sections.map((section) => ({
    ...section,
    illustrations: uniqueImages(section.illustrations, seenStoryImages),
  }));
  const hasSectionImages = sections?.some(
    (section) => section.illustrations?.length,
  );
  const readable =
    !!selected &&
    (!selected.report_id ||
      (report?.status === "completed" && !!report.report));
  const reviewStatus = report?.report?.review_status;
  const reviewNotice = reviewStatus === "partial" || reviewStatus === "unavailable"
    ? report?.report?.review_notice?.trim() || (reviewStatus === "partial"
      ? "部分重点信息尚未完成核对，可结合原始来源阅读。"
      : "重点信息核对暂不可用，可结合原始来源阅读。")
    : null;
  return (
    <div className="feed-layout">
      <section className="feed-main">
        <div className="page-heading">
          <div>
            <h1>
              主场动态
              <span className="heading-dot" />
            </h1>
            <p>值得知道的消息，都在这里。</p>
          </div>
        </div>
        <WorkflowControls kind="news" teamId={team.id} onChange={workflowChanged} onConfigure={openModel} />
        {preparation?.team_id === team.id && preparation.status !== "idle" && (
          <div className="notice" role="status" aria-live="polite">
            <div>
              <p>{preparation.message}</p>
              {!!preparation.source_errors?.length && <details>
                <summary>查看 {preparation.source_errors.length} 个来源说明</summary>
                <SourceGapNotice gaps={preparation.source_errors} />
              </details>}
            </div>
          </div>
        )}
        {preparationError && <div className="notice error" role="alert">消息来源准备未完成：{preparationError}</div>}
        <div className="feed-toolbar">
          <div className="filter-tabs">
            {topics.map((item) => (
              <button
                key={item.id}
                className={topic === item.id ? "active" : ""}
                aria-pressed={topic === item.id}
                onClick={() => setTopic(item.id)}
              >
                {item.name}
              </button>
            ))}
          </div>
          <span className="feed-order">最新发布</span>
        </div>
        {error && (
          <div className="notice error" role="alert">
            {error}
            <button onClick={() => load()}>重试</button>
          </div>
        )}
        {!feed && loading ? (
          <div className="feed-skeleton" aria-label="正在读取动态">
            {[0, 1, 2].map((i) => (
              <div key={i}>
                <span />
                <i />
                <i />
                <b />
              </div>
            ))}
          </div>
        ) : (
          <div className="feed-list">
            {items.map((item) => (
              <article className="feed-card" key={item.id}>
                <div className="feed-card-meta">
                  <Crest team={team} small />
                  <div>
                    <strong>{team.name}</strong>
                    <span>{item.organized_at ? `整理于${friendlyTime(item.organized_at) || "时间未知"}`
                      : item.published_at ? `来源发布于${friendlyTime(item.published_at)}`
                        : item.published_date ? `来源发布于 ${item.published_date}（仅日期）` : "发布时间未知"}</span>
                  </div>
                  <span className={`topic-tag topic-${item.topic}`}>
                    {topicName(item.topic)}
                  </span>
                </div>
                <h2>
                  <button onClick={() => read(item)}>{item.title}</button>
                </h2>
                <div className="feed-summary">
                  <Markdown text={item.summary} evidence={item.sources} />
                </div>
                {!!item.source_gap_count && (
                  <p className="feed-source-gap" role="note">
                    {item.source_gap_count} 个来源暂时未能读取，展开后可查看原因。
                  </p>
                )}
                {!!item.images?.length && (
                  <div
                    className={`feed-images images-${Math.min(item.images.length, 3)}`}
                  >
                    {item.images.slice(0, 3).map((image, i) => (
                      <Photo key={image.url || i} image={image} />
                    ))}
                  </div>
                )}
                <Sources sources={item.sources} showDates />
                <div className="feed-card-actions">
                  <button
                    className="text-button"
                    disabled={!!asking}
                    onClick={() => askAbout(item)}
                  >
                    <MessageCircle size={16} />
                    问问助手
                  </button>
                  {item.report_id && (
                    <button
                      className="text-button read-more"
                      onClick={() => read(item)}
                    >
                      展开阅读
                      <ChevronDown size={15} />
                    </button>
                  )}
                </div>
              </article>
            ))}
          </div>
        )}
        {feed && !items.length && (
          <div className="empty-state feed-empty">
            <Newspaper size={35} />
            <h2>{topic ? "这个分类还没有新消息" : preparation?.status === "running" ? "正在准备球队消息"
              : preparation?.counts?.readable ? "消息来源已就绪"
              : preparation?.status === "failed" ? "消息来源暂时未能读取"
              : preparation?.status === "empty" ? "暂未找到可整理的新消息" : "这里还没有中文动态"}</h2>
            <p>{topic ? "看看其他分类，或手动更新球队消息。"
              : preparation?.status === "running" ? "正在查找公开消息并读取正文，完成后会更新来源状态。"
              : preparation?.counts?.readable ? `已取得 ${preparation.counts.readable} 条可读来源。点击上方“启动”，整理成中文动态。`
              : preparation?.status === "empty" ? "已检查当前可用来源。暂无新消息时会保留已有资料，稍后可再次更新。"
              : "点击上方“启动”重新检查来源并整理中文动态。来源或整理失败的原因会显示在上方。"}</p>
          </div>
        )}
        {feed?.next_cursor && (
          <button
            className="button load-more"
            disabled={loading}
            onClick={() => load(feed.next_cursor!)}
          >
            {loading ? (
              <Spinner text="加载中" />
            ) : (
              <>
                查看更多动态
                <ChevronDown size={16} />
              </>
            )}
          </button>
        )}
        {!!items.length && !feed?.next_cursor && (
          <div className="feed-end">
            <span />
            你已读完当前动态
            <span />
          </div>
        )}
      </section>
      <aside className="feed-aside">
        {upcoming && (
          <section className="next-match side-card">
            <span className="small-heading">
              <CalendarDays size={14} />
              下一场比赛
            </span>
            <div className="next-match-clubs">
              <div>
                <Crest team={team} />
                <strong>{team.name}</strong>
              </div>
              <span>VS</span>
              <div>
                <Crest
                  team={findClub(upcoming.opponent)}
                  name={upcoming.opponent}
                />
                <strong>{clubName(upcoming.opponent)}</strong>
              </div>
            </div>
            <p>
              {dateTime(upcoming.kickoff)}
              <small>北京时间</small>
            </p>
            <button
              className="button"
              onClick={() =>
                ask({
                  text: `为我做一份${team.name}对阵${upcoming.opponent}的赛前前瞻。`,
                  skill: "match_preview",
                  matchId: upcoming.id,
                })
              }
            >
              聊聊这场比赛
              <ArrowRight size={14} />
            </button>
          </section>
        )}
        <section className="assistant-invitation side-card">
          <span className="assistant-orb">
            <Sparkles size={23} />
          </span>
          <h3>
            不止看见，
            <br />
            更想看懂。
          </h3>
          <p>转会的影响、阵容的变化、比赛的转折。把好奇交给助手。</p>
          <button
            onClick={() =>
              ask({
                text: `帮我梳理${team.name}今天值得关注的消息，并解释对球队的影响。`,
                skill: "team_brief",
              })
            }
          >
            今日球队速览
            <ArrowRight size={16} />
          </button>
        </section>
        <p className="aside-note">
          你的球队，你的节奏。
          <br />
          消息由助手整理，点来源可继续了解。
        </p>
      </aside>
      {selected && (
        <Modal
          title={
            selected.report_id
              ? readable
                ? report!.report!.title
                : "消息详情"
              : selected.title
          }
          onClose={() => {
            readSerial.current++;
            setSelected(null);
          }}
          wide
        >
          <article className="story-reader">
            <div className="story-meta">
              <span>{team.name}</span>
              <span>{selected.organized_at ? `整理于${friendlyTime(selected.organized_at) || "时间未知"}`
                : selected.published_at ? `来源发布于${friendlyTime(selected.published_at)}` : "发布时间未知"}</span>
              <span>{topicName(selected.topic)}</span>
            </div>
            {reading ? (
              <Spinner text="正在展开消息" />
            ) : (
              <>
                {readError && (
                  <div className="notice error">
                    {readError}
                    <button onClick={() => read(selected)}>重试</button>
                  </div>
                )}
                {readable && (
                  <>
                    <SourceGapNotice gaps={report?.report?.source_gaps} />
                    {reviewNotice ? (
                      <p className="story-review-notice" role="note">{reviewNotice}</p>
                    ) : reviewStatus === "checked" ? (
                      <p className="story-review-checked">重点信息已核对</p>
                    ) : null}
                    <p className="story-lead">
                      {selected.report_id
                        ? report?.report?.lead
                        : selected.summary}
                    </p>
                    <div className="story-images">
                      {uniqueImages(
                        hasSectionImages
                          ? []
                          : selected.report_id
                            ? report?.report?.images || []
                            : selected.images || [],
                      )
                        .slice(0, 1)
                        .map((image) => (
                          <Photo key={image.url} image={image} />
                        ))}
                    </div>
                    {sections?.map((section, i) => (
                      <section className="story-section" key={i}>
                        <h3>{section.heading}</h3>
                        <Markdown
                          text={section.body}
                          evidence={report?.report?.sources}
                        />
                        {section.illustrations?.map((image) => (
                          <Photo key={image.url} image={image} />
                        ))}
                        <Sources
                          showDates
                          sources={report?.report?.sources.filter((source) =>
                            section.evidence_ids?.includes(source.id),
                          )}
                        />
                      </section>
                    ))}
                    {report?.report?.analysis && (
                      <section className="story-analysis">
                        <h3>
                          <Sparkles size={17} />
                          这意味着什么
                        </h3>
                        <Markdown
                          text={report.report.analysis}
                          evidence={report.report.sources}
                        />
                      </section>
                    )}
                    {!!report?.report?.next_steps?.length && (
                      <section className="story-section">
                        <h3>接下来关注</h3>
                        <ul>
                          {report.report.next_steps.map((step, i) => (
                            <li key={i}>{step}</li>
                          ))}
                        </ul>
                      </section>
                    )}
                    <Sources
                      showDates
                      sources={
                        selected.report_id
                          ? report?.report?.sources
                          : selected.sources
                      }
                    />
                    <button
                      className="button primary story-ask"
                      onClick={() => {
                        void askAbout(selected);
                        setSelected(null);
                      }}
                    >
                      <MessageCircle size={16} />
                      继续问助手
                    </button>
                  </>
                )}
              </>
            )}
          </article>
        </Modal>
      )}
    </div>
  );
});
