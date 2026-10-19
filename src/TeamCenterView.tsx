import { memo, useCallback, useEffect, useRef, useState } from "react";
import {
  ArrowRight,
  ArrowUpRight,
  CalendarDays,
  ChevronDown,
  MapPin,
  Search,
  Shield,
  Sparkles,
  Star,
  Target,
  Trophy,
  UserRound,
  Users,
} from "lucide-react";
import { api, errorText } from "./api";
import { DecisionHistory } from "./DecisionHistory";
import { TeamHeroActions } from "./TeamActions";
import { Crest, Modal, safeUrl, Spinner } from "./ui";
import { findClub } from "./clubCatalog";
import { Sources, friendlyTime, type SourceLink } from "./shared";
import type { AppState, Team } from "./types";
import type { AssistantPrompt } from "./AssistantView";

interface Player {
  id: string;
  name: string;
  /** Chinese reading of the name, present only when it is a known reading.
   *  The interface shows it beside `name`, never instead of it. */
  name_zh?: string;
  name_zh_source?: string;
  name_zh_source_url?: string;
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
  photo_file_page?: string;
  photo_license?: string;
  photo_restrictions?: string;
  photo_match?: string;
  photo_reason?: string;
  photo_updated_at?: string;
  source_url?: string;
  bio?: string;
  stats?: Record<string, number | string>;
}
/** One board of the squad's own season leaders, as the server ranked them. */
interface Standout {
  id: string;
  name: string;
  name_zh?: string;
  number?: string | number;
  position?: string;
  position_group?: string;
  photo_url?: string;
  photo_credit?: string;
  photo_source_url?: string;
  photo_file_page?: string;
  photo_license?: string;
  photo_match?: string;
  photo_reason?: string;
  appearances?: number;
  value: number;
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
  competition_id?: string;
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
interface Standing extends SeasonStats {
  rank?: number;
  team_id?: string;
  team_name?: string;
  goal_difference?: number;
  is_current?: boolean;
}
interface CenterData {
  team: Team;
  league?: { name?: string };
  season?: { id?: string; label?: string; start_year?: number };
  roster_season?: { id: string; label: string; start_year: number } | null;
  latest_season?: { id: string; label: string; start_year: number } | null;
  roster_fallback?: boolean;
  roster: Player[];
  matches: TeamMatch[];
  standouts?: Record<string, Standout[]>;
  standings?: Standing[];
  standings_hidden?: boolean;
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
  /** PR #4 panel charts: null blocks mean SAMPLE_INSUFFICIENT / SCOPE_DENIED empty state. */
  chart_blocks?: {
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
      home?: SeasonStats & { goals_for?: number; goals_against?: number };
      away?: SeasonStats & { goals_for?: number; goals_against?: number };
    } | null;
  } | null;
  updated_at?: string;
  roster_updated_at?: string;
  // 后端 center_view 返回的三个分项时间戳，用作"资料真的变了"的指纹。
  // updated_at 是它们的最大值，所以它本身不能当指纹：只要有任何一项
  // 变动，它每次轮询都会被当成新版本。
  fixtures_updated_at?: string;
  team_results_updated_at?: string;
  team_schedule_updated_at?: string;
  standings_updated_at?: string;
  refresh_status?: string | { status?: string };
  errors?: string[];
  sources?: SourceLink[];
  components?: Partial<Record<"roster" | "schedule" | "standings" | "portraits" | "profile", {
    status: string;
    updated_at: string | null;
    stale: boolean;
    available: boolean;
    errors: string[];
    available_count?: number;
    total_count?: number;
    untried_count?: number;
    settled_count?: number;
    no_identity_facts_count?: number;
    acceptance_note?: string;
    review_note?: string;
    missing_fields?: string[];
    fields?: Record<string, string | number | null | undefined>;
    note?: string;
    crest?: { status: string; url: string; local_asset: boolean; source_url: string; attribution: string; missing: string[]; note: string };
  }>>;
  coverage?: { matches: number; expected_matches: number | null; has_full_schedule: boolean; results_complete: boolean | null; basis: string };
  preparation?: { status: string; message: string; counts?: { players?: number; photos?: number } };
}
const componentNames = { roster: "球员阵容", schedule: "比赛赛程", standings: "积分榜", portraits: "球员照片", profile: "球队资料" };
const componentStatuses: Record<string, string> = {
  missing: "等待收录", fresh: "已更新", stale: "待更新", running: "更新中", partial: "部分收录", failed: "更新未完成", unavailable: "来源暂无资料",
};
const results: Record<string, string> = { W: "胜", D: "平", L: "负" };
const numberOrDash = (value?: number) =>
  value === undefined || value === null ? "—" : value;
const WEEKDAYS = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"];
/**
 * A kickoff in Beijing time, split into the parts the fixture row shows.
 *
 * Every read is pinned to Asia/Shanghai rather than the browser's zone: the
 * dates come from the source in UTC and "which day is this" is a question the
 * reader answers in local football time, not in whatever the machine is set to.
 * The weekday is derived from the same pinned date for the same reason — asking
 * the browser for the weekday of a UTC instant is how a Sunday match ends up
 * labelled Saturday on a machine in another zone.
 */
const localStamp = (value?: string | null) => {
  const date = value ? new Date(value) : null;
  if (!date || Number.isNaN(date.getTime())) return null;
  const zone = "Asia/Shanghai";
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone: zone, month: "numeric", day: "numeric", weekday: "short",
    // ``hourCycle: "h23"`` rather than ``hour12: false``: the latter yields
    // "24:00" for midnight in some locales, and a fixture listed at 24:30 is a
    // time that does not exist.
    hour: "2-digit", minute: "2-digit", hourCycle: "h23",
  }).formatToParts(date);
  const pick = (type: string) => parts.find((part) => part.type === type)?.value || "";
  const weekdayShort = pick("weekday");
  const index = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"].indexOf(weekdayShort);
  const month = Number(pick("month"));
  const day = Number(pick("day"));
  return {
    date,
    month: `${month} 月`,
    day: `${day} 日`,
    weekday: index >= 0 ? WEEKDAYS[index] : "",
    time: `${pick("hour")}:${pick("minute")}`,
  };
};
/** Whole days between now and a kickoff, or null when there is nothing to count. */
const daysUntil = (value?: string | null) => {
  if (!value) return null;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return null;
  return Math.ceil((date.getTime() - Date.now()) / 86400000);
};

export const TeamCenter = memo(function TeamCenter({
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
  const [tab, setTab] = useState("info");
  const [position, setPosition] = useState("");
  const [playerQuery, setPlayerQuery] = useState("");
  const [player, setPlayer] = useState<Player | null>(null);
  const [matchTab, setMatchTab] = useState("upcoming");
  const [matchLimit, setMatchLimit] = useState(8);
  const [revealedTeam, setRevealedTeam] = useState<string | null>(null);
  const [fillOpen, setFillOpen] = useState(false);
  const reveal = revealedTeam === team.id;
  const active = useRef(team.id);
  active.current = team.id;
  const request = useRef(0);
  const reading = useRef(false);
  const load = useCallback(async () => {
    const serial = ++request.current;
    reading.current = true;
    setLoading(true);
    try {
      const next = await api<CenterData>(
        `/teams/${encodeURIComponent(team.id)}/center${reveal ? "?reveal=true" : ""}`,
      );
      if (serial !== request.current || active.current !== team.id) return;
      setCenter(next);
      setPlayer((current) => current ? next.roster.find((item) => item.id === current.id) || null : null);
      setError("");
    } catch (err) {
      if (serial === request.current) setError(errorText(err));
    } finally {
      if (serial === request.current) {
        reading.current = false;
        setLoading(false);
      }
    }
  }, [team.id, reveal]);
  useEffect(() => {
    setCenter(null);
    setTab("info");
    setPosition("");
    setPlayerQuery("");
    setPlayer(null);
    setRevealedTeam(null);
    setFillOpen(false);
  }, [team.id]);
  useEffect(() => {
    void load();
    return () => {
      request.current++;
    };
  }, [load]);
  useEffect(() => {
    let disposed = false;
    void api(`/teams/${encodeURIComponent(team.id)}/prepare`, {
      method: "POST", body: JSON.stringify({ kind: "team" }),
    }).then(() => { if (!disposed) void load(); }).catch((err) => {
      if (!disposed) setError(errorText(err));
    });
    return () => { disposed = true; };
    // Preparation is tied to opening a club, independent of tabs or reveal.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [team.id]);
  // 轮询节奏只看"有没有在跑"，所以依赖一个稳定的布尔值，而不是
  // components 对象本身——每次响应都会重建它，那样定时器会被不停地
  // 清掉重设，永远等不到下一次触发。
  const busy = center?.preparation?.status === "running" ||
    Object.values(center?.components || {}).some((item) => item?.status === "running");
  useEffect(() => {
    const refreshVisible = () => {
      if (document.visibilityState === "visible" && !reading.current) void load();
    };
    const timer = window.setInterval(refreshVisible, busy ? 5000 : 60000);
    document.addEventListener("visibilitychange", refreshVisible);
    window.addEventListener("online", refreshVisible);
    return () => {
      clearInterval(timer);
      document.removeEventListener("visibilitychange", refreshVisible);
      window.removeEventListener("online", refreshVisible);
    };
  }, [load, busy]);
  const workflowChanged = useCallback(() => {
    void load();
  }, [load]);
  const profile = { ...findClub(team.id), ...team, ...center?.team };
  const season = center?.stats.season;
  const standingsHidden = center?.stats.hidden || center?.standings_hidden;
  const standings = [...(center?.standings || [])].sort(
    (a, b) => (a.rank ?? Infinity) - (b.rank ?? Infinity),
  );
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
  /** Chart labels sit ten to a row, so an opponent is written the way a fan
 *  would write it in a table: the club's short name when the catalogue has
 *  one, otherwise the first four characters of the Chinese name. */
function clubShortName(value?: string | null) {
  if (!value) return "";
  const club = findClub(value);
  const short = club?.short_name?.trim();
  if (short) return short;
  const name = club?.name || value;
  return name.length > 4 ? name.slice(0, 4) : name;
}

const normalizeName = (value: string) => value.normalize("NFD").replace(/\p{M}/gu, "").toLowerCase().trim();
  const query = normalizeName(playerQuery);
  const roster = (center?.roster || []).filter((item) =>
    (!position || item.position_group === position) &&
    (!query || [item.name, item.name_zh || ""].some((name) => normalizeName(name).includes(query))),
  );
  const goals = (center?.charts?.goals_by_match || []).slice(-10);
  const maxGoals = Math.max(
    1,
    ...goals.flatMap((item) => [item.goals_for, item.goals_against]),
  );
  const blocks = center?.chart_blocks;
  const blockEmpty = !!blocks && (blocks.results == null || blocks.status === "empty");
  const blockReady = !!blocks && blocks.status === "ready" && !!blocks.results;
  const resultBars = blockReady
    ? [
        { label: "胜", value: blocks!.results!.won || 0 },
        { label: "平", value: blocks!.results!.drawn || 0 },
        { label: "负", value: blocks!.results!.lost || 0 },
      ]
    : !blocks && center?.charts?.results?.length
      ? center.charts.results
      : !blocks && season
        ? [
            { label: "胜", value: season.won || 0 },
            { label: "平", value: season.drawn || 0 },
            { label: "负", value: season.lost || 0 },
          ]
        : [];
  const resultTotal = resultBars.reduce((sum, item) => sum + (item.value || 0), 0);
  const chartSampleNote = blocks?.sample?.scope
    ? `样本范围：${blocks.sample.scope}${blocks.sample.played != null ? ` · ${blocks.sample.played} 场` : ""}`
    : blocks?.reason_code === "SAMPLE_INSUFFICIENT"
      ? "样本不足"
      : blocks?.reason_code === "SCOPE_DENIED"
        ? "避剧透下不展示图表数字"
        : "样本范围：已收录完整比分的联赛场次";
  const homeBlock = blocks?.home_away?.home;
  const awayBlock = blocks?.home_away?.away;
  const showHomeAway = blockReady
    ? !!(homeBlock || awayBlock)
    : !blocks && !!season?.played;
  const recentForm = (center?.stats.recent || []).slice(-5);
  const capacity = (center?.team as { stadium_capacity?: number } | undefined)?.stadium_capacity ?? (profile as { stadium_capacity?: number }).stadium_capacity;
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
            <h1>{team.name}</h1>
            <p className="club-english">{profile.english_name}</p>
            <div className="club-facts">
              {profile.founded && <span>创立于 {profile.founded}</span>}
              {profile.stadium && (
                <span>
                  <MapPin size={13} />
                  {profile.stadium}
                  {capacity ? ` · ${capacity.toLocaleString("zh-CN")} 座` : ""}
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
        <TeamHeroActions
          teamId={team.id}
          updatedAt={center?.updated_at}
          center={center}
          onChanged={workflowChanged}
          fillOpen={fillOpen}
          onToggleFill={() => setFillOpen((value) => !value)}
        />
      </section>
      {(!center || (!center.roster.length && !center.matches.length)) && (
        <div className="material-status" role="status">
          <Spinner text="正在准备球队的名单、照片与比赛资料" />
        </div>
      )}
      <div className="center-toolbar">
        <div className="filter-tabs">
          {[
            { id: "info", label: "球队信息" },
            { id: "squad", label: "球员信息" },
            { id: "fixtures", label: "未来比赛" },
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
          {standingsHidden && (
            <button
              className="text-button"
              onClick={() => setRevealedTeam(team.id)}
            >
              显示比赛结果
            </button>
          )}
        </div>
      </div>
      {center && !center.roster.length && !center.matches.length && (
        <div className="material-status" aria-label="球队资料收录情况">
          <span>
            <Users size={13} /> {center.roster.length} 名已收录球员
          </span>
          {center.roster_season && (
            <span>当前为 {center.roster_season.label} 赛季名单</span>
          )}
          <span>
            <CalendarDays size={13} /> {center.matches.length} 场比赛记录
          </span>
          <span>
            <Shield size={13} /> {center.sources?.length || 0} 个资料来源
          </span>
          <small>
            {center.updated_at
              ? `资料更新于${friendlyTime(center.updated_at)}`
              : "等待资料同步"}
          </small>
        </div>
      )}
      <DecisionHistory teamId={team.id} version={[
        center?.roster_updated_at, center?.fixtures_updated_at, center?.standings_updated_at,
        center?.refresh_status,
      ].join("|")} />
      {center?.coverage && <p className="team-coverage">
        本赛季联赛已收录 {center.coverage.matches}{center.coverage.expected_matches != null ? ` / ${center.coverage.expected_matches}` : ""} 场赛程。
        {center.coverage.expected_matches != null && !center.coverage.has_full_schedule && "其余赛程待补充。"}
        {center.coverage.basis}
      </p>}
      {error && (
        <div className="notice error" role="alert">
          {error}
          <button onClick={load}>重试</button>
        </div>
      )}
      {!center && loading && (
        <div className="page-loading">
          <Spinner text="正在读取球队档案" />
        </div>
      )}
      {tab === "info" && (
        <>
          <section className="panel club-identity">
            <div className="section-heading">
              <h2>球队资料</h2>
            </div>
            <div className="identity-grid">
              {[
                { label: "所在城市", value: profile.city },
                { label: "国家 / 地区", value: profile.country },
                { label: "成立年份", value: profile.founded ? `${profile.founded} 年` : "" },
                { label: "主场", value: profile.stadium },
                { label: "球场容量", value: capacity ? `${capacity.toLocaleString("zh-CN")} 座` : "" },
                { label: "昵称", value: profile.nickname },
                { label: "所属联赛", value: center?.league?.name || team.league_name || team.league },
                { label: "英文名", value: profile.english_name },
              ]
                .filter((item) => item.value)
                .map((item) => (
                  <div key={item.label}>
                    <span>{item.label}</span>
                    <strong>{item.value}</strong>
                  </div>
                ))}
            </div>
            {profile.description && <p className="identity-intro">{profile.description}</p>}
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
                    {recentForm.length ? (
                      recentForm.map((result, i) => (
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
          <section className="panel team-standings-panel">
            <div className="section-heading">
              <h2>联赛积分榜</h2>
              <span>{center?.league?.name || team.league_name || team.league}</span>
            </div>
            {standingsHidden ? (
              <div className="empty-state compact">
                <Shield size={24} />
                <p>积分榜已随比赛结果隐藏。</p>
              </div>
            ) : standings.length ? (
              <div className="team-standings-scroll" tabIndex={0} role="region" aria-label="联赛积分榜，可横向滚动查看全部数据">
                <table className="team-standings-table" aria-label="联赛积分榜">
                  <thead>
                    <tr>
                      {["排名", "球队", "已赛", "胜", "平", "负", "进球", "失球", "净胜球", "积分"].map((label) => (
                        <th key={label} scope="col">{label}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {standings.map((standing) => {
                      const current = standing.team_id === team.id || standing.is_current === true;
                      const club = current ? profile : data.teams.find((item) => item.id === standing.team_id) || findClub(standing.team_id || "");
                      return (
                        <tr key={standing.team_id || `${standing.rank}-${standing.team_name}`} className={current ? "is-current" : undefined} aria-current={current || undefined}>
                          <td>{numberOrDash(standing.rank)}</td>
                          <th scope="row">
                            <div className="team-standing-club">
                              <Crest team={club} name={standing.team_name} small />
                              <span>{club?.name || standing.team_name || "球队名称待补充"}</span>
                              {current && <small>当前球队</small>}
                            </div>
                          </th>
                          <td>{numberOrDash(standing.played)}</td>
                          <td>{numberOrDash(standing.won)}</td>
                          <td>{numberOrDash(standing.drawn)}</td>
                          <td>{numberOrDash(standing.lost)}</td>
                          <td>{numberOrDash(standing.goals_for)}</td>
                          <td>{numberOrDash(standing.goals_against)}</td>
                          <td>{numberOrDash(standing.goal_difference)}</td>
                          <td><strong>{numberOrDash(standing.points)}</strong></td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            ) : (
              <div className="empty-state compact">
                <Trophy size={24} />
                <p>{loading ? "正在读取联赛积分榜。" : "暂无联赛积分榜，启动球队资料更新后会在这里显示。"}</p>
              </div>
            )}
          </section>
          {!center?.stats.hidden && (
            <div className="charts-grid charts-grid-three">
              <section className="panel results-panel">
                <div className="section-heading">
                  <h2>胜平负</h2>
                  <span>联赛样本</span>
                </div>
                {!blockEmpty && resultTotal > 0 ? (
                  <>
                    <div
                      className="performance-track results-track"
                      role="img"
                      aria-label={resultBars.map((item) => `${item.label}${item.value}`).join(" ")}
                    >
                      {resultBars.map((item) => (
                        <i
                          key={item.label}
                          className={`result-${item.label === "胜" ? "W" : item.label === "平" ? "D" : "L"}`}
                          style={{ width: `${(item.value / resultTotal) * 100}%` }}
                        />
                      ))}
                    </div>
                    <div className="chart-legend results-legend">
                      {resultBars.map((item) => (
                        <span key={item.label}>
                          <i className={`legend-${item.label === "胜" ? "W" : item.label === "平" ? "D" : "L"}`} />
                          {item.label} <b>{item.value}</b>
                        </span>
                      ))}
                    </div>
                    <p className="chart-basis-note">{chartSampleNote}</p>
                  </>
                ) : (
                  <div className="chart-empty chart-empty-dashed">
                    <p>{blocks?.reason_code === "SCOPE_DENIED" ? "避剧透未展示" : "样本不足"}</p>
                  </div>
                )}
              </section>
              <section className="panel goals-panel">
                <div className="section-heading">
                  <h2>进球与防守</h2>
                  <span>最近 {goals.length || "—"} 场联赛</span>
                </div>
                {(blocks ? blocks.goals != null : true) && goals.length ? (
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
                          <span className="goal-chart-label" title={item.label}>
                            {clubShortName(item.label)}
                            <small>{item.date ? String(item.date).slice(5, 10) : ""}</small>
                          </span>
                        </div>
                      ))}
                    </div>
                  </>
                ) : (
                  <div className="chart-empty chart-empty-dashed">
                    <p>样本不足</p>
                  </div>
                )}
              </section>
              <section className="panel performance-panel">
                <div className="section-heading">
                  <h2>主客场表现</h2>
                  <span>联赛数据</span>
                </div>
                {showHomeAway ? (
                  <>
                    {[
                      { label: "主场", value: blockReady ? homeBlock : center?.stats.home },
                      { label: "客场", value: blockReady ? awayBlock : center?.stats.away },
                    ].map((item) => {
                      const played = item.value?.played || 0;
                      // ``chart_blocks.home_away`` carries won/drawn/lost but not
                      // points, and it is preferred over ``stats.home`` when the
                      // chart blocks are ready — so reading ``points`` straight
                      // off the chosen value shows "—" for a split whose record
                      // is known. Points from wins and draws are arithmetic, not
                      // a source claim, so they are derived here rather than
                      // leaving a visible hole in an otherwise complete row.
                      const points = item.value?.points ??
                        (item.value ? (item.value.won || 0) * 3 + (item.value.drawn || 0) : undefined);
                      return (
                        <div className="performance-row" key={item.label}>
                          <div>
                            <strong>{item.label}</strong>
                            <span>
                              {played} 场 · {numberOrDash(points)}{" "}
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
                        <strong>{numberOrDash(blockReady ? blocks?.goals?.goals_for : season?.goals_for)}</strong>
                        <span>总进球</span>
                      </div>
                      <div>
                        <strong>{numberOrDash(blockReady ? blocks?.goals?.goals_against : season?.goals_against)}</strong>
                        <span>总失球</span>
                      </div>
                      <div>
                        <strong>
                          {(blockReady ? blocks?.goals?.goals_for : season?.goals_for) !== undefined &&
                          (blockReady ? blocks?.goals?.goals_against : season?.goals_against) !== undefined
                            ? (blockReady ? (blocks!.goals!.goals_for || 0) : (season!.goals_for || 0)) -
                              (blockReady ? (blocks!.goals!.goals_against || 0) : (season!.goals_against || 0))
                            : "—"}
                        </strong>
                        <span>净胜球</span>
                      </div>
                    </div>
                    {blockReady && chartSampleNote && (
                      <p className="chart-basis-note">{chartSampleNote}</p>
                    )}
                  </>
                ) : (
                  <div className="chart-empty chart-empty-dashed">
                    <p>样本不足</p>
                  </div>
                )}
              </section>
            </div>
          )}
          {!center?.stats.hidden && <Standouts boards={center?.standouts} onOpen={(board, item) => {
            const found = (center?.roster || []).find((row) => row.id === item.id);
            if (found) setPlayer(found);
            else if (board) setPlayer(item as unknown as Player);
          }} />}
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
              <strong>让助手把这组数据讲给你听</strong>
              <p>结合本季表现，说说优势在哪、哪里需要补强。</p>
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
                {center?.roster_season?.label || center?.season?.label || "当前赛季"} ·{" "}
                {center?.roster.length || 0} 名球员
              </p>
              {center?.roster_fallback && (
                <p>
                  当前为 {center?.roster_season?.label} 赛季名单；
                  {center?.latest_season?.label} 赛季球队目录尚未齐全。
                </p>
              )}
            </div>
          </div>
          <label className="player-search">
            <Search size={17} aria-hidden="true" />
            <input type="search" aria-label="搜索球员" placeholder="搜索中文名或原名" value={playerQuery}
              onChange={(event) => setPlayerQuery(event.target.value)} />
            <span aria-live="polite">{roster.length} / {center?.roster.length || 0}</span>
          </label>
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
                aria-pressed={position === item.id}
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
                  {chineseReading(item) ? (
                    <h3 lang="zh-Hans">
                      {chineseReading(item)}
                      <small className="player-name-zh">{item.name}</small>
                    </h3>
                  ) : (
                    // No Chinese reading yet. Falling back to the source spelling
                    // alone, not "中文名待补": the English name is how the source
                    // writes this player, and a placeholder would print a fault
                    // notice on a card whose only problem is a pending pass.
                    <h3>{item.name}</h3>
                  )}
                  <p>
                    {[item.nationality, item.age ? `${item.age} 岁` : ""]
                      .filter(Boolean)
                      .join(" · ")}
                  </p>
                  {seasonLine(item) && <p className="player-season-line">{seasonLine(item)}</p>}
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
              <h3>{center?.roster.length ? "没有匹配的球员" : "还没有可读的阵容资料"}</h3>
              <p>{center?.roster.length ? "试试中文名、原名，或查看全部位置。" : "点击上方“启动”更新一次球队资料，已确认的球员会出现在这里。"}</p>
              {!!center?.roster.length && <button className="button" onClick={() => { setPosition(""); setPlayerQuery(""); }}>查看全部球员</button>}
            </div>
          )}
        </section>
      )}
      {tab === "fixtures" && (
        <section className="panel schedule-panel">
          <div className="section-heading">
            <div>
              <h2>{matchTab === "recent" ? "已完成比赛" : "未来比赛"}</h2>
              <p>{center?.season?.label || "当前赛季"} · 北京时间</p>
            </div>
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
          <div className={matchTab === "upcoming" ? "fixture-timeline" : "match-list"}>
            {matches.slice(0, matchLimit).map((match) => (
              <FixtureRow
                key={match.id}
                match={match}
                upcoming={matchTab === "upcoming"}
                teams={data.teams}
                onAsk={() =>
                  ask({
                    text: `请${matchTab === "recent" ? "复盘" : "前瞻"}${team.name}对阵${match.opponent}的比赛。`,
                    skill: matchTab === "recent" ? "match_review" : "match_preview",
                    matchId: match.id,
                  })
                }
              />
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
              <div className="player-detail-photo">
                {player.number ? (
                  <span className="player-number">{player.number}</span>
                ) : null}
                <PlayerPhoto player={player} allowRetry />
              </div>
              <div className="player-detail-intro">
                {chineseReading(player) && <p className="player-name-zh">{chineseReading(player)}</p>}
                <span className="pill">
                  {player.position} · {team.name}
                </span>
                {player.bio && <p className="player-bio">{player.bio}</p>}
              </div>
            </div>
            {playerPhotoUrl(player) && (
              <p className="player-photo-attribution">
                照片：{player.photo_credit || "图片来源"}
                {player.photo_license && <> · {player.photo_license}</>}
                {safeUrl(player.photo_file_page) && (
                  <> · <a href={safeUrl(player.photo_file_page)!} target="_blank" rel="noopener noreferrer">查看照片文件页 <ArrowUpRight size={12} /></a></>
                )}
                {!player.photo_file_page && safeUrl(player.photo_source_url) && (
                  <> · <a href={safeUrl(player.photo_source_url)!} target="_blank" rel="noopener noreferrer">查看照片来源 <ArrowUpRight size={12} /></a></>
                )}
                {player.photo_restrictions && <small>用途提示：{player.photo_restrictions.replace(/\bpersonality\b/gi, "人物肖像权可能另有限制")}</small>}
              </p>
            )}
            {!playerPhotoUrl(player) && player.photo_reason && (
              <p className="player-photo-reason">暂无可核验照片：{player.photo_reason}</p>
            )}
            <dl>
              {[
                ["中文名", chineseReading(player)],
                ["国籍", player.nationality],
                ["年龄", player.age ? `${player.age} 岁` : undefined],
                ["身高", player.height_cm ? `${player.height_cm} cm` : undefined],
                ["体重", player.weight_kg ? `${player.weight_kg} kg` : undefined],
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
                  { id: "shots", label: "射门" },
                  { id: "shots_on_target", label: "射正" },
                  { id: "yellow_cards", label: "黄牌" },
                  { id: "red_cards", label: "红牌" },
                  { id: "saves", label: "扑救" },
                  { id: "goals_conceded", label: "失球" },
                ]
                  .filter((item) => player.stats?.[item.id] !== undefined && player.stats?.[item.id] !== "")
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
                ...(chineseReading(player) && safeUrl(player.name_zh_source_url)
                  ? [{ name: `中文名${player.name_zh_source ? ` · ${player.name_zh_source}` : "来源"}`, url: player.name_zh_source_url }]
                  : []),
                ...(player.photo_file_page || player.photo_source_url
                  ? [
                      {
                        name: "照片来源",
                        url: player.photo_file_page || player.photo_source_url,
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
});

function chineseReading(player: Pick<Player, "name_zh">) {
  const reading = player.name_zh?.trim();
  return reading || undefined;
}

/**
 * A player's season totals as one short line, or nothing.
 *
 * The squad source writes an absent total as ``null`` rather than omitting it —
 * a player with no appearances has ``{"appearances": null, "goals": null}`` —
 * so a check that only rejects ``undefined`` and ``""`` renders the string
 * "null" into the card. Every value is tested for being a real number here, and
 * a total of zero is dropped: "0 球" beside every defender is noise, and the
 * line only earns its place when it says something the position does not.
 */
function seasonLine(player: Player) {
  const stats = player.stats || {};
  const whole = (value: unknown) =>
    typeof value === "number" && Number.isFinite(value) ? value : null;
  const parts: string[] = [];
  const appearances = whole(stats.appearances);
  if (appearances !== null && appearances > 0) parts.push(`${appearances} 场`);
  for (const [field, label] of [["goals", "球"], ["assists", "助攻"], ["saves", "扑救"]] as const) {
    const value = whole(stats[field]);
    if (value !== null && value > 0) parts.push(`${value} ${label}`);
  }
  return parts.length ? `本季 ${parts.join(" · ")}` : "";
}

function playerPhotoUrl(player: Player) {
  if (!safeUrl(player.photo_source_url)) return null;
  if (player.photo_match?.startsWith("wikimedia_") &&
      (!player.photo_credit?.trim() || !player.photo_license?.trim() || !safeUrl(player.photo_file_page))) return null;
  return safeUrl(player.photo_url);
}

/**
 * The squad's own season leaders, on three separate boards.
 *
 * Read from the squad source's per-player totals, which is also why the boards
 * are split: a keeper's saves and a forward's goals are different units, so a
 * single ranking would be sorted on a number that means something different for
 * each row. A board with nothing on it is not rendered at all — a heading over
 * an empty list reads as a fault, and "nobody has scored yet" is a normal state
 * six games into a season.
 */
function Standouts({ boards, onOpen }: { boards?: Record<string, Standout[]>; onOpen: (board: string, item: Standout) => void }) {
  const boardMeta: { id: string; title: string; unit: string; icon: typeof Target }[] = [
    { id: "goals", title: "队内射手", unit: "球", icon: Target },
    { id: "assists", title: "助攻榜", unit: "次", icon: Star },
    { id: "saves", title: "门将扑救", unit: "次", icon: Shield },
  ];
  const shown = boardMeta.filter((meta) => (boards?.[meta.id] || []).length);
  if (!shown.length) return null;
  return (
    <section className="panel standouts-panel">
      <div className="section-heading">
        <div>
          <h2>本季亮眼球员</h2>
          <p>按来源给出的本季累计数据排序，未做换算</p>
        </div>
      </div>
      <div className={`standouts-grid standouts-grid-${shown.length}`}>
        {shown.map((meta) => {
          const Icon = meta.icon;
          return (
            <div className="standout-board" key={meta.id}>
              <h3>
                <Icon size={15} />
                {meta.title}
              </h3>
              <ol>
                {(boards?.[meta.id] || []).map((item, index) => (
                  <li key={item.id || index}>
                    <button onClick={() => onOpen(meta.id, item)}>
                      <span className="standout-rank">{index + 1}</span>
                      <span className="standout-photo">
                        <PlayerPhoto player={item as unknown as Player} />
                      </span>
                      <span className="standout-name">
                        <strong>{item.name_zh || item.name}</strong>
                        {(item.name_zh || item.position) && (
                          <small>
                            {[item.name_zh ? item.name : "", item.position]
                              .filter(Boolean)
                              .join(" · ")}
                          </small>
                        )}
                      </span>
                      <span className="standout-value">
                        <strong>{item.value}</strong>
                        <small>{meta.unit}</small>
                      </span>
                    </button>
                  </li>
                ))}
              </ol>
            </div>
          );
        })}
      </div>
    </section>
  );
}

/**
 * One fixture, as a timeline entry rather than a table row.
 *
 * The kickoff is the thing a reader is scanning for, so it gets its own column
 * with the weekday: "10月18日" answers nothing on its own when the question is
 * "how long until this". A countdown is shown only for a kickoff the source has
 * actually confirmed — an unconfirmed date with a countdown beside it would
 * present a guess as a schedule.
 */
function FixtureRow({
  match,
  upcoming,
  teams,
  onAsk,
}: {
  match: TeamMatch;
  upcoming: boolean;
  teams: Team[];
  onAsk: () => void;
}) {
  const stamp = localStamp(match.kickoff);
  const opponent = teams.find((item) => item.id === match.opponent_id) || findClub(match.opponent);
  const countdown = upcoming && match.kickoff_confirmed !== false ? daysUntil(match.kickoff) : null;
  const countdownText = countdown === null
    ? ""
    : countdown <= 0
      ? "即将开始"
      : countdown === 1
        ? "明天"
        : `${countdown} 天后`;
  return (
    <article className={`fixture-row${match.kickoff_confirmed === false ? " is-tentative" : ""}`}>
      <div className="fixture-when">
        {stamp ? (
          <>
            <strong>{stamp.month}{stamp.day}</strong>
            <span>{stamp.weekday}</span>
            <small>{stamp.time}</small>
          </>
        ) : (
          <>
            <strong>待定</strong>
            <span>时间未公布</span>
          </>
        )}
      </div>
      <div className="fixture-body">
        <div className="fixture-teams">
          <span className={`venue-tag${match.venue === "home" ? " is-home" : match.venue === "away" ? " is-away" : ""}`}>
            {match.venue === "home" ? "主" : match.venue === "away" ? "客" : "—"}
          </span>
          <Crest team={opponent} name={match.opponent} small />
          <div className="fixture-opponent">
            <strong>{opponent?.name || match.opponent}</strong>
            {match.competition && <small>{match.competition}</small>}
          </div>
          {(match.spoiler_hidden ||
            (match.goals_for != null && match.goals_against != null)) && (
            <span className={`match-score ${match.result ? `result-text-${match.result}` : ""}`}>
              {match.spoiler_hidden
                ? "已隐藏"
                : `${match.goals_for} : ${match.goals_against}`}
            </span>
          )}
        </div>
        <div className="fixture-meta">
          {countdownText && <span className="fixture-countdown">{countdownText}</span>}
          {match.kickoff_confirmed === false && <span className="fixture-tentative">开球时间待确认</span>}
        </div>
      </div>
      <button className="text-button fixture-action" onClick={onAsk}>
        {upcoming ? "前瞻" : "复盘"}
        <ArrowRight size={14} />
      </button>
    </article>
  );
}

/**
 * Why some players have no photo. "17 张没有" reads as a failure, but the same
 * gap means different things: players a source cannot match at all are a
 * settled answer, and players never reached yet are simply not done.
 */
function photoGapNote(part: { available_count?: number; total_count?: number; untried_count?: number;
  settled_count?: number; no_identity_facts_count?: number }) {
  const missing = (part.total_count || 0) - (part.available_count || 0);
  if (missing <= 0) return "";
  const parts: string[] = [];
  if (part.untried_count) parts.push(`${part.untried_count} 人待核对`);
  if (part.settled_count) parts.push(`${part.settled_count} 人来源暂无可用照片`);
  const blocked = part.no_identity_facts_count
    ? `；其中 ${part.no_identity_facts_count} 人缺少国籍与出生日期，来源无法核对身份，需先补全阵容资料`
    : "";
  // A server that has not sent the breakdown yet still reports the gap; say so
  // rather than showing nothing under a visible shortfall.
  return parts.length ? `缺 ${missing} 张：${parts.join("，")}${blocked}` : `还缺 ${missing} 张有来源的照片。`;
}
export function PlayerPhoto({ player, allowRetry = false }: { player: Player; allowRetry?: boolean }) {
  const [failed, setFailed] = useState(false);
  const [attempt, setAttempt] = useState(0);
  useEffect(() => setFailed(false), [player.id, player.photo_url]);
  const url = playerPhotoUrl(player);
  return url && !failed ? (
    <img
      key={`${player.id}:${attempt}`}
      className="player-photo"
      src={url}
      alt={player.name}
      loading="lazy"
      decoding="async"
      referrerPolicy="no-referrer"
      onError={() => setFailed(true)}
    />
  ) : (
    <div className="player-photo player-no-photo" title={!url ? player.photo_reason : undefined}>
      <UserRound size={55} strokeWidth={1} />
      <span>{url && failed ? "照片暂时无法加载" : "暂无照片"}</span>
      {url && failed && allowRetry && <button className="text-button" onClick={() => { setAttempt((value) => value + 1); setFailed(false); }}>重试照片</button>}
    </div>
  );
}
