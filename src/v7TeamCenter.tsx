import { useCallback, useEffect, useRef, useState } from "react";
import {
  ArrowRight,
  ArrowUpRight,
  CalendarDays,
  ChevronDown,
  MapPin,
  RefreshCw,
  Shield,
  Sparkles,
  Trophy,
  UserRound,
  Users,
} from "lucide-react";
import { api, errorText, post } from "./api";
import { Crest, Modal, safeUrl, Spinner } from "./components";
import { findClub } from "./clubCatalog";
import { Sources, friendlyTime, type SourceLink } from "./v7Shared";
import type { AppState, Team } from "./types";
import type { AssistantPrompt } from "./v7Assistant";

interface Player {
  id: string;
  name: string;
  number?: string | number;
  position: string;
  position_group: string;
  nationality?: string;
  birth_date?: string;
  age?: number;
  height_cm?: number;
  weight_kg?: number;
  photo_url?: string;
  photo_credit?: string;
  photo_source_url?: string;
  photo_updated_at?: string;
  source_url?: string;
  bio?: string;
  stats?: Record<string, number | string>;
}
interface TeamMatch {
  id: string;
  opponent: string;
  opponent_id?: string;
  venue?: string;
  kickoff?: string;
  kickoff_confirmed?: boolean;
  status: string;
  goals_for?: number;
  goals_against?: number;
  result?: string;
  source_url?: string;
  competition?: string;
  spoiler_hidden?: boolean;
}
interface SeasonStats {
  played?: number;
  won?: number;
  drawn?: number;
  lost?: number;
  goals_for?: number;
  goals_against?: number;
  points?: number;
  win_rate?: number;
}
interface CenterData {
  team: Team;
  league?: { name?: string };
  season?: { id?: string; label?: string; start_year?: number };
  roster: Player[];
  matches: TeamMatch[];
  stats: {
    hidden?: boolean;
    sample_complete?: boolean;
    season?: SeasonStats;
    home?: SeasonStats;
    away?: SeasonStats;
    recent?: string[];
    standing?: { rank?: number; played?: number; points?: number } | null;
  };
  charts?: {
    goals_by_match?: {
      id: string;
      label: string;
      date?: string;
      goals_for: number;
      goals_against: number;
    }[];
    results?: { label: string; value: number }[];
    home_away?: ({ label: string } & SeasonStats)[];
  };
  updated_at?: string;
  roster_updated_at?: string;
  refresh_status?: string | { status?: string };
  errors?: string[];
  sources?: SourceLink[];
}
const results: Record<string, string> = { W: "胜", D: "平", L: "负" };
const numberOrDash = (value?: number) =>
  value === undefined || value === null ? "—" : value;

export function TeamCenter({
  team,
  data,
  ask,
}: {
  team: Team;
  data: AppState;
  ask: (value: AssistantPrompt) => void;
}) {
  const [center, setCenter] = useState<CenterData | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [tab, setTab] = useState("overview");
  const [position, setPosition] = useState("");
  const [player, setPlayer] = useState<Player | null>(null);
  const [matchTab, setMatchTab] = useState("upcoming");
  const [matchLimit, setMatchLimit] = useState(8);
  const [revealedTeam, setRevealedTeam] = useState<string | null>(null);
  const reveal = revealedTeam === team.id;
  const active = useRef(team.id);
  active.current = team.id;
  const request = useRef(0);
  const load = useCallback(async () => {
    const serial = ++request.current;
    setLoading(true);
    try {
      const next = await api<CenterData>(
        `/teams/${encodeURIComponent(team.id)}/center${reveal ? "?reveal=true" : ""}`,
      );
      if (serial !== request.current || active.current !== team.id) return;
      setCenter(next);
      setError("");
      const status =
        typeof next.refresh_status === "object"
          ? next.refresh_status.status
          : next.refresh_status;
      setRefreshing(["running", "queued", "refreshing"].includes(status || ""));
    } catch (err) {
      if (serial === request.current) setError(errorText(err));
    } finally {
      if (serial === request.current) setLoading(false);
    }
  }, [team.id, reveal]);
  useEffect(() => {
    setCenter(null);
    setTab("overview");
    setPosition("");
    setPlayer(null);
    setRevealedTeam(null);
  }, [team.id]);
  useEffect(() => {
    void load();
    return () => {
      request.current++;
    };
  }, [load]);
  useEffect(() => {
    if (!refreshing) return;
    const timer = window.setInterval(() => void load(), 5000);
    return () => clearInterval(timer);
  }, [refreshing, load]);
  const refresh = async () => {
    if (refreshing) return;
    setRefreshing(true);
    try {
      await post(`/teams/${encodeURIComponent(team.id)}/maintain`);
      await load();
    } catch (err) {
      setError(errorText(err));
      setRefreshing(false);
    }
  };
  const profile = { ...findClub(team.id), ...team, ...center?.team };
  const season = center?.stats.season;
  const matches = (center?.matches || [])
    .filter((match) =>
      matchTab === "recent"
        ? match.status === "finished" || match.status === "completed"
        : match.status !== "finished" && match.status !== "completed",
    )
    .sort((a, b) =>
      matchTab === "recent"
        ? (b.kickoff || "").localeCompare(a.kickoff || "")
        : (a.kickoff || "").localeCompare(b.kickoff || ""),
    );
  const roster = (center?.roster || []).filter(
    (item) => !position || item.position_group === position,
  );
  const goals = (center?.charts?.goals_by_match || []).slice(-10);
  const maxGoals = Math.max(
    1,
    ...goals.flatMap((item) => [item.goals_for, item.goals_against]),
  );
  return (
    <div className="team-center">
      <section className="club-hero">
        <div className="club-hero-art" aria-hidden="true">
          <i />
          <b />
          <span />
        </div>
        <div className="club-hero-content">
          <div className="club-hero-crest">
            <Crest team={profile} />
          </div>
          <div>
            <span className="eyebrow">THE CLUB, UP CLOSE</span>
            <h1>{team.name}</h1>
            <p className="club-english">{profile.english_name}</p>
            <div className="club-facts">
              {profile.founded && <span>创立于 {profile.founded}</span>}
              {profile.stadium && (
                <span>
                  <MapPin size={13} />
                  {profile.stadium}
                </span>
              )}
              <span>
                {center?.league?.name || team.league_name || team.league}
              </span>
            </div>
          </div>
        </div>
        <div className="club-hero-footer">
          <span>{profile.nickname || "每一场比赛，都有新的故事"}</span>
          {safeUrl(profile.official_url) && (
            <a
              href={safeUrl(profile.official_url)!}
              target="_blank"
              rel="noopener noreferrer"
            >
              球队官网
              <ArrowUpRight size={14} />
            </a>
          )}
        </div>
      </section>
      <div className="center-toolbar">
        <div className="filter-tabs">
          {[
            { id: "overview", label: "球队概览" },
            { id: "squad", label: "球员阵容" },
            { id: "schedule", label: "比赛日程" },
          ].map((item) => (
            <button
              key={item.id}
              className={tab === item.id ? "active" : ""}
              aria-pressed={tab === item.id}
              onClick={() => setTab(item.id)}
            >
              {item.label}
            </button>
          ))}
        </div>
        <div className="center-controls">
          {center?.stats.hidden && (
            <button
              className="text-button"
              onClick={() => setRevealedTeam(team.id)}
            >
              显示比赛结果
            </button>
          )}
          <button
            className="icon-button"
            disabled={loading || refreshing}
            onClick={refresh}
            aria-label="更新球队资料"
            title="更新球队资料"
          >
            <RefreshCw size={17} className={refreshing ? "spin" : ""} />
          </button>
        </div>
      </div>
      {error && (
        <div className="notice error" role="alert">
          {error}
          <button onClick={load}>重试</button>
        </div>
      )}
      {refreshing && (
        <div className="feed-progress" role="status">
          <Spinner text="正在更新球队资料" />
        </div>
      )}
      {!center && loading && (
        <div className="page-loading">
          <Spinner text="正在整理球队档案" />
        </div>
      )}
      {tab === "overview" && (
        <>
          <div className="club-overview-grid">
            <section className="panel club-about">
              <div className="section-heading">
                <h2>关于{team.name}</h2>
                <Shield size={18} />
              </div>
              <p>
                {profile.description ||
                  "球队介绍正在更新，你也可以向助手了解球队的历史与风格。"}
              </p>
              <div className="club-details">
                {profile.city && (
                  <div>
                    <span>城市</span>
                    <strong>{profile.city}</strong>
                  </div>
                )}
                {profile.stadium && (
                  <div>
                    <span>主场</span>
                    <strong>{profile.stadium}</strong>
                  </div>
                )}
                {profile.country && (
                  <div>
                    <span>国家</span>
                    <strong>{profile.country}</strong>
                  </div>
                )}
              </div>
              <button
                className="text-button"
                onClick={() =>
                  ask({
                    text: `为我介绍${team.name}的球队历史、战术风格和目前最值得关注的球员，注明资料来源。`,
                    skill: "team_maintenance",
                  })
                }
              >
                <Sparkles size={15} />
                让助手带我认识这支球队
                <ArrowRight size={14} />
              </button>
            </section>
            <section className="panel season-card">
              <div className="section-heading">
                <h2>赛季表现</h2>
                <span>{center?.season?.label || "当前赛季"}</span>
              </div>
              {center?.stats.hidden ? (
                <div className="empty-state compact">
                  <Shield size={24} />
                  <p>已为你隐藏比赛结果。</p>
                </div>
              ) : (
                <>
                  <div className="season-numbers">
                    <div>
                      <strong>
                        {numberOrDash(center?.stats.standing?.rank)}
                      </strong>
                      <span>联赛排名</span>
                    </div>
                    <div>
                      <strong>
                        {numberOrDash(center?.stats.standing?.points)}
                      </strong>
                      <span>积分</span>
                    </div>
                    <div>
                      <strong>
                        {numberOrDash(
                          center?.stats.standing?.played ?? season?.played,
                        )}
                      </strong>
                      <span>已赛</span>
                    </div>
                  </div>
                  <div className="result-line">
                    <span>近期状态</span>
                    <div>
                      {center?.stats.recent?.length ? (
                        center.stats.recent.slice(-5).map((result, i) => (
                          <b key={i} className={`result-${result}`}>
                            {results[result] || result}
                          </b>
                        ))
                      ) : (
                        <small>暂无已完成比赛</small>
                      )}
                    </div>
                  </div>
                  {center?.stats.sample_complete === false && (
                    <p className="sample-note">
                      下方表现统计已收录 {season?.played || 0} 场比赛
                    </p>
                  )}
                  <div className="win-draw-loss">
                    <span>
                      <i className="result-W" />
                      {numberOrDash(season?.won)} 胜
                    </span>
                    <span>
                      <i className="result-D" />
                      {numberOrDash(season?.drawn)} 平
                    </span>
                    <span>
                      <i className="result-L" />
                      {numberOrDash(season?.lost)} 负
                    </span>
                  </div>
                </>
              )}
            </section>
          </div>
          {!center?.stats.hidden && (
            <div className="charts-grid">
              <section className="panel goals-panel">
                <div className="section-heading">
                  <h2>进球与防守</h2>
                  <span>最近 {goals.length || "—"} 场联赛</span>
                </div>
                {goals.length ? (
                  <>
                    <div className="chart-legend">
                      <span>
                        <i className="goals-for" />
                        进球
                      </span>
                      <span>
                        <i className="goals-against" />
                        失球
                      </span>
                    </div>
                    <div
                      className="goal-chart"
                      role="img"
                      aria-label={goals
                        .map(
                          (item) =>
                            `${item.label}：进球${item.goals_for}，失球${item.goals_against}`,
                        )
                        .join("；")}
                    >
                      {goals.map((item) => (
                        <div className="goal-chart-column" key={item.id}>
                          <div className="goal-bar-pair">
                            <div
                              className="goal-bar goals-for"
                              style={{
                                height: `${Math.max(item.goals_for > 0 ? 6 : 1, (item.goals_for / maxGoals) * 100)}%`,
                              }}
                            >
                              <span>{item.goals_for}</span>
                            </div>
                            <div
                              className="goal-bar goals-against"
                              style={{
                                height: `${Math.max(item.goals_against > 0 ? 6 : 1, (item.goals_against / maxGoals) * 100)}%`,
                              }}
                            >
                              <span>{item.goals_against}</span>
                            </div>
                          </div>
                          <span className="goal-chart-label">{item.label}</span>
                        </div>
                      ))}
                    </div>
                  </>
                ) : (
                  <div className="chart-empty">
                    <Trophy size={25} />
                    <p>有了正式比赛数据，表现就会在这里呈现。</p>
                  </div>
                )}
              </section>
              <section className="panel performance-panel">
                <div className="section-heading">
                  <h2>主客场表现</h2>
                  <span>联赛数据</span>
                </div>
                {season?.played ? (
                  <>
                    {[
                      { label: "主场", value: center?.stats.home },
                      { label: "客场", value: center?.stats.away },
                    ].map((item) => {
                      const played = item.value?.played || 0;
                      return (
                        <div className="performance-row" key={item.label}>
                          <div>
                            <strong>{item.label}</strong>
                            <span>
                              {played} 场 · {numberOrDash(item.value?.points)}{" "}
                              分
                            </span>
                          </div>
                          <div
                            className="performance-track"
                            role="img"
                            aria-label={`${item.label}：${item.value?.won || 0}胜${item.value?.drawn || 0}平${item.value?.lost || 0}负`}
                          >
                            <i
                              className="result-W"
                              style={{
                                width: `${played ? ((item.value?.won || 0) / played) * 100 : 0}%`,
                              }}
                            />
                            <i
                              className="result-D"
                              style={{
                                width: `${played ? ((item.value?.drawn || 0) / played) * 100 : 0}%`,
                              }}
                            />
                            <i
                              className="result-L"
                              style={{
                                width: `${played ? ((item.value?.lost || 0) / played) * 100 : 0}%`,
                              }}
                            />
                          </div>
                          <small>
                            {item.value?.won || 0} 胜 / {item.value?.drawn || 0}{" "}
                            平 / {item.value?.lost || 0} 负
                          </small>
                        </div>
                      );
                    })}
                    <div className="goals-total">
                      <div>
                        <strong>{numberOrDash(season.goals_for)}</strong>
                        <span>总进球</span>
                      </div>
                      <div>
                        <strong>{numberOrDash(season.goals_against)}</strong>
                        <span>总失球</span>
                      </div>
                      <div>
                        <strong>
                          {season.goals_for !== undefined &&
                          season.goals_against !== undefined
                            ? season.goals_for - season.goals_against
                            : "—"}
                        </strong>
                        <span>净胜球</span>
                      </div>
                    </div>
                  </>
                ) : (
                  <div className="chart-empty">
                    <p>暂无主客场统计。</p>
                  </div>
                )}
              </section>
            </div>
          )}
          {center?.stats.sample_complete === false && !center.stats.hidden && (
            <p className="chart-basis-note">
              比赛表现按已收录的完整比分统计，积分以联赛积分榜为准。
            </p>
          )}
          <button
            className="team-assistant-banner"
            onClick={() =>
              ask({
                text: `结合本赛季数据，分析${team.name}的表现、优势与需要改善的地方。`,
                skill: "team_maintenance",
              })
            }
          >
            <span>
              <Sparkles size={22} />
            </span>
            <div>
              <strong>数字背后的比赛，交给助手来读。</strong>
              <p>从阵容到战术，深入了解这支球队。</p>
            </div>
            <ArrowRight size={20} />
          </button>
        </>
      )}
      {tab === "squad" && (
        <section className="squad-section">
          <div className="section-heading">
            <div>
              <h2>一线队阵容</h2>
              <p>
                {center?.season?.label || "当前赛季"} ·{" "}
                {center?.roster.length || 0} 名球员
              </p>
            </div>
            <Users size={21} />
          </div>
          <div className="position-tabs filter-tabs">
            {[
              { id: "", name: "全部位置" },
              { id: "GK", name: "门将" },
              { id: "DF", name: "后卫" },
              { id: "MF", name: "中场" },
              { id: "FW", name: "前锋" },
            ].map((item) => (
              <button
                key={item.id}
                className={position === item.id ? "active" : ""}
                onClick={() => setPosition(item.id)}
              >
                {item.name}
              </button>
            ))}
          </div>
          <div className="player-grid">
            {roster.map((item) => (
              <button
                key={item.id}
                className="player-card"
                onClick={() => setPlayer(item)}
              >
                <div className="player-visual">
                  <span className="player-number">{item.number ?? ""}</span>
                  <PlayerPhoto player={item} />
                  <span className="player-position">{item.position}</span>
                </div>
                <div className="player-card-info">
                  <h3>{item.name}</h3>
                  <p>
                    {[item.nationality, item.age ? `${item.age} 岁` : ""]
                      .filter(Boolean)
                      .join(" · ")}
                  </p>
                  <span>
                    了解球员
                    <ArrowRight size={13} />
                  </span>
                </div>
              </button>
            ))}
          </div>
          {!roster.length && !loading && (
            <div className="empty-state">
              <Users size={31} />
              <h3>阵容资料正在补齐</h3>
              <p>已确认的球员和照片会出现在这里。</p>
              <button
                className="button"
                onClick={refresh}
                disabled={refreshing}
              >
                更新阵容
              </button>
            </div>
          )}
        </section>
      )}
      {tab === "schedule" && (
        <section className="panel schedule-panel">
          <div className="section-heading">
            <div>
              <h2>比赛日程</h2>
              <p>{center?.season?.label || "当前赛季"} · 北京时间</p>
            </div>
            <CalendarDays size={20} />
          </div>
          <div className="filter-tabs">
            <button
              className={matchTab === "upcoming" ? "active" : ""}
              onClick={() => {
                setMatchTab("upcoming");
                setMatchLimit(8);
              }}
            >
              即将开赛
            </button>
            <button
              className={matchTab === "recent" ? "active" : ""}
              onClick={() => {
                setMatchTab("recent");
                setMatchLimit(8);
              }}
            >
              已完成
            </button>
          </div>
          <div className="match-list">
            {matches.slice(0, matchLimit).map((match) => (
              <div className="match-row" key={match.id}>
                <div className="match-date">
                  <strong>
                    {match.kickoff
                      ? new Date(match.kickoff).toLocaleDateString("zh-CN", {
                          month: "numeric",
                          day: "numeric",
                        })
                      : "待定"}
                  </strong>
                  <span>
                    {match.kickoff
                      ? new Date(match.kickoff).toLocaleTimeString("zh-CN", {
                          hour: "2-digit",
                          minute: "2-digit",
                          hour12: false,
                          timeZone: "Asia/Shanghai",
                        })
                      : ""}
                    {match.kickoff_confirmed === false ? " · 待确认" : ""}
                  </span>
                </div>
                <span className="venue-tag">
                  {match.venue === "home"
                    ? "主"
                    : match.venue === "away"
                      ? "客"
                      : "—"}
                </span>
                <div className="match-opponent">
                  <Crest
                    team={
                      data.teams.find(
                        (item) => item.id === match.opponent_id,
                      ) || findClub(match.opponent)
                    }
                    name={match.opponent}
                    small
                  />
                  <strong>
                    {findClub(match.opponent)?.name || match.opponent}
                  </strong>
                </div>
                <span
                  className={`match-score ${match.result ? `result-text-${match.result}` : ""}`}
                >
                  {match.goals_for != null &&
                  match.goals_against != null &&
                  !match.spoiler_hidden
                    ? `${match.goals_for} : ${match.goals_against}`
                    : match.spoiler_hidden
                      ? "已隐藏"
                      : "VS"}
                </span>
                <button
                  className="text-button"
                  onClick={() =>
                    ask({
                      text: `请${matchTab === "recent" ? "复盘" : "前瞻"}${team.name}对阵${match.opponent}的比赛。`,
                      skill:
                        matchTab === "recent"
                          ? "match_review"
                          : "match_preview",
                      matchId: match.id,
                    })
                  }
                >
                  {matchTab === "recent" ? "复盘" : "前瞻"}
                  <ArrowRight size={14} />
                </button>
              </div>
            ))}
          </div>
          {!matches.length && !loading && (
            <div className="empty-state compact">
              <CalendarDays size={27} />
              <p>
                {matchTab === "recent"
                  ? "暂时没有已完成的比赛。"
                  : "暂时没有后续赛程。"}
              </p>
            </div>
          )}
          {matches.length > matchLimit && (
            <button
              className="button load-more"
              onClick={() => setMatchLimit(matchLimit + 12)}
            >
              查看更多比赛
              <ChevronDown size={15} />
            </button>
          )}
        </section>
      )}
      {center && (
        <footer className="team-sources">
          <Sources sources={center.sources} />
          {center.updated_at && (
            <span>更新于{friendlyTime(center.updated_at)}</span>
          )}
        </footer>
      )}
      {player && (
        <Modal title={player.name} onClose={() => setPlayer(null)}>
          <div className="player-detail">
            <div className="player-detail-hero">
              <span>{player.number}</span>
              <PlayerPhoto player={player} />
            </div>
            <span className="pill">
              {player.position} · {team.name}
            </span>
            {player.bio && <p className="player-bio">{player.bio}</p>}
            <dl>
              {[
                ["国籍", player.nationality],
                ["年龄", player.age ? `${player.age} 岁` : undefined],
                [
                  "身高",
                  player.height_cm ? `${player.height_cm} cm` : undefined,
                ],
                [
                  "体重",
                  player.weight_kg ? `${player.weight_kg} kg` : undefined,
                ],
                ["出生日期", player.birth_date?.slice(0, 10)],
              ]
                .filter(([, value]) => value)
                .map(([label, value]) => (
                  <div key={label}>
                    <dt>{label}</dt>
                    <dd>{value}</dd>
                  </div>
                ))}
            </dl>
            {player.stats && Object.keys(player.stats).length > 0 && (
              <div className="player-stat-row">
                {[
                  { id: "appearances", label: "出场" },
                  { id: "goals", label: "进球" },
                  { id: "assists", label: "助攻" },
                ]
                  .filter((item) => player.stats?.[item.id] !== undefined)
                  .map((item) => (
                    <div key={item.id}>
                      <strong>{player.stats?.[item.id]}</strong>
                      <span>{item.label}</span>
                    </div>
                  ))}
              </div>
            )}
            <Sources
              sources={[
                { name: "球员资料", url: player.source_url },
                ...(player.photo_source_url
                  ? [
                      {
                        name: player.photo_credit || "照片来源",
                        url: player.photo_source_url,
                      },
                    ]
                  : []),
              ]}
            />
            <button
              className="button primary"
              onClick={() => {
                ask({
                  text: `介绍${team.name}的${player.name}：技术特点、在球队中的角色和近期表现，请结合可靠资料。`,
                  skill: "team_maintenance",
                });
                setPlayer(null);
              }}
            >
              <Sparkles size={16} />
              聊聊这位球员
            </button>
          </div>
        </Modal>
      )}
    </div>
  );
}

function PlayerPhoto({ player }: { player: Player }) {
  const [failed, setFailed] = useState(false);
  const url = safeUrl(player.photo_url);
  return url && !failed ? (
    <img
      className="player-photo"
      src={url}
      alt={player.name}
      loading="lazy"
      referrerPolicy="no-referrer"
      onError={() => setFailed(true)}
    />
  ) : (
    <div className="player-photo player-no-photo">
      <UserRound size={55} strokeWidth={1} />
      <span>暂无照片</span>
    </div>
  );
}
