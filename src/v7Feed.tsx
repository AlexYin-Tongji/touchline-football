import { useCallback, useEffect, useRef, useState } from "react";
import {
  ArrowRight,
  CalendarDays,
  ChevronDown,
  LoaderCircle,
  MessageCircle,
  Newspaper,
  RefreshCw,
  Sparkles,
} from "lucide-react";
import { api, errorText, post } from "./api";
import { Crest, dateTime, Modal, Spinner } from "./components";
import { findClub } from "./clubCatalog";
import { clubName } from "./teamHub";
import {
  friendlyTime,
  Markdown,
  Photo,
  Sources,
  type MediaImage,
  type SourceLink,
} from "./v7Shared";
import type { AppState, AutoReport, Team } from "./types";
import type { AssistantPrompt } from "./v7Assistant";

interface FeedItem {
  id: string;
  title: string;
  summary: string;
  topic?: string;
  updated_at?: string;
  images?: MediaImage[];
  sources?: SourceLink[];
  report_id?: string | null;
  status?: string;
  team_id: string;
}
interface FeedResponse {
  items: FeedItem[];
  next_cursor: string | null;
  refresh?: {
    status?: string;
    last_updated?: string;
    error?: string;
    pending?: number;
  };
  pending_count?: number;
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

export function FeedView({
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
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState("");
  const [selected, setSelected] = useState<FeedItem | null>(null);
  const [report, setReport] = useState<IllustratedReport | null>(null);
  const [reading, setReading] = useState(false);
  const [readError, setReadError] = useState("");
  const serial = useRef(0);
  const readSerial = useRef(0);
  const activeTeam = useRef(team.id);
  activeTeam.current = team.id;
  const load = useCallback(
    async (cursor?: string) => {
      const request = ++serial.current;
      const requestedTeam = team.id;
      setLoading(true);
      try {
        const result = await api<FeedResponse>(
          `/feed?team_id=${encodeURIComponent(requestedTeam)}&limit=20${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}`,
        );
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
        setRefreshing(
          result.refresh?.status === "running" ||
            result.refresh?.status === "queued",
        );
      } catch (err) {
        if (request === serial.current) setError(errorText(err));
      } finally {
        if (request === serial.current) setLoading(false);
      }
    },
    [team.id],
  );
  useEffect(() => {
    setFeed(null);
    setTopic("");
    setSelected(null);
    setRefreshing(false);
    void load();
    return () => {
      serial.current++;
    };
  }, [load]);
  useEffect(() => {
    const timer = window.setInterval(
      () => {
        if (
          document.visibilityState === "visible" &&
          (feed?.items.length || 0) <= 20
        )
          void load();
      },
      refreshing ? 4000 : 60000,
    );
    return () => clearInterval(timer);
  }, [refreshing, load, feed?.items.length]);
  const refresh = async () => {
    if (refreshing) return;
    const requestedTeam = team.id;
    setRefreshing(true);
    setError("");
    try {
      await post("/feed/refresh", { team_id: requestedTeam });
      if (activeTeam.current === requestedTeam) await load();
    } catch (err) {
      if (activeTeam.current === requestedTeam) {
        setError(errorText(err));
        setRefreshing(false);
      }
    }
  };
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
        if (!result.report) setReadError("这条消息正在更新，请稍后刷新动态。");
      }
    } catch (err) {
      if (request === readSerial.current) setReadError(errorText(err));
    } finally {
      if (request === readSerial.current) setReading(false);
    }
  };
  const items = (feed?.items || []).filter(
    (item) => !topic || item.topic === topic,
  );
  const upcoming = data.matches
    .filter(
      (item) =>
        item.team_id === team.id &&
        item.status !== "finished" &&
        item.kickoff &&
        Date.parse(item.kickoff) >= Date.now(),
    )
    .sort((a, b) => (a.kickoff || "").localeCompare(b.kickoff || ""))[0];
  const pending = feed?.pending_count || feed?.refresh?.pending || 0;
  const hasSectionImages = report?.report?.sections.some(
    (section) => section.illustrations?.length,
  );
  return (
    <div className="feed-layout">
      <section className="feed-main">
        <div className="page-heading">
          <div>
            <span className="eyebrow">YOUR DAILY TOUCHLINE</span>
            <h1>
              主场动态
              <span className="heading-dot" />
            </h1>
            <p>值得知道的消息，都在这里。</p>
          </div>
          <button
            className="button refresh-button"
            disabled={refreshing || loading}
            onClick={refresh}
          >
            <RefreshCw size={15} className={refreshing ? "spin" : ""} />
            <span>{refreshing ? "更新中" : "刷新动态"}</span>
          </button>
        </div>
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
        {(refreshing || pending > 0) && (
          <div className="feed-progress" role="status">
            {refreshing ? (
              <LoaderCircle size={15} className="spin" />
            ) : (
              <Sparkles size={15} />
            )}
            <span>
              {refreshing
                ? pending > 0
                  ? `正在整理 ${pending} 条新消息`
                  : "正在为你查找和整理新消息"
                : `${pending} 条新消息待整理`}
            </span>
            {!data.setup?.model_configured && (
              <button className="text-button" onClick={openModel}>
                连接模型
                <ArrowRight size={13} />
              </button>
            )}
          </div>
        )}
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
                    <span>{friendlyTime(item.updated_at)}</span>
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
                {!!item.images?.length && (
                  <div
                    className={`feed-images images-${Math.min(item.images.length, 3)}`}
                  >
                    {item.images.slice(0, 3).map((image, i) => (
                      <Photo key={image.url || i} image={image} />
                    ))}
                  </div>
                )}
                <Sources sources={item.sources} />
                <div className="feed-card-actions">
                  <button
                    className="text-button"
                    onClick={() =>
                      ask({
                        text: `请进一步解释这条关于${team.name}的消息：${item.title}`,
                        skill: "auto",
                        evidenceIds: item.sources?.flatMap((source) =>
                          source.id ? [source.id] : [],
                        ),
                      })
                    }
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
            <h2>{topic ? "这个分类还没有新消息" : "主场消息，正在等你开启"}</h2>
            <p>
              {topic
                ? "看看其他分类，或稍后再来。"
                : "看台会从球队和媒体来源中整理中文动态。"}
            </p>
            {!topic && (
              <button
                className="button primary"
                disabled={refreshing}
                onClick={refresh}
              >
                {refreshing ? "正在整理" : "获取最新动态"}
                <ArrowRight size={16} />
              </button>
            )}
            {!data.setup?.model_configured && !topic && (
              <button className="text-button" onClick={openModel}>
                连接模型，开启自动整理
              </button>
            )}
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
        <Modal title={selected.title} onClose={() => setSelected(null)} wide>
          <article className="story-reader">
            <div className="story-meta">
              <span>{team.name}</span>
              <span>{friendlyTime(selected.updated_at)}</span>
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
                {(!report || report.report) && (
                  <p className="story-lead">
                    {report?.report?.lead || selected.summary}
                  </p>
                )}
                <div className="story-images">
                  {(hasSectionImages
                    ? []
                    : report?.report?.images || selected.images || []
                  )
                    .slice(0, 1)
                    .map((image) => (
                      <Photo key={image.url} image={image} />
                    ))}
                </div>
                {report?.report?.sections.map((section, i) => (
                  <section className="story-section" key={i}>
                    <h3>{section.heading}</h3>
                    <Markdown
                      text={section.body}
                      evidence={report.report?.sources}
                    />
                    {section.illustrations?.map((image) => (
                      <Photo key={image.url} image={image} />
                    ))}
                    <Sources
                      sources={report.report?.sources.filter((source) =>
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
                  sources={report?.report?.sources || selected.sources}
                />
                <button
                  className="button primary story-ask"
                  onClick={() => {
                    ask({
                      text: `关于“${selected.title}”，请帮我进一步分析对${team.name}的影响。`,
                      skill: "auto",
                      evidenceIds: selected.sources?.flatMap((source) =>
                        source.id ? [source.id] : [],
                      ),
                    });
                    setSelected(null);
                  }}
                >
                  <MessageCircle size={16} />
                  继续问助手
                </button>
              </>
            )}
          </article>
        </Modal>
      )}
    </div>
  );
}
