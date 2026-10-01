import { useCallback, useEffect, useRef, useState } from "react";
import {
  ArrowRight,
  ArrowUpRight,
  CalendarDays,
  ChevronDown,
  MapPin,
  Search,
  Shield,
  Sparkles,
  Trophy,
  UserRound,
  Users,
} from "lucide-react";
import { api, errorText } from "./api";
import { WorkflowControls } from "./WorkflowControls";
import { DecisionHistory } from "./DecisionHistory";
import { Crest, Modal, safeUrl, Spinner } from "./components";
import { findClub } from "./clubCatalog";
import { Sources, friendlyTime, type SourceLink } from "./v7Shared";
import type { AppState, Team } from "./types";
import type { AssistantPrompt } from "./v7Assistant";

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
  updated_at?: string;
  roster_updated_at?: string;
  refresh_status?: string | { status?: string };
  errors?: string[];
  sources?: SourceLink[];
  components?: Partial<Record<"roster" | "schedule" | "standings" | "portraits", {
    status: string;
    updated_at: string | null;
    stale: boolean;
    available: boolean;
    errors: string[];
    available_count?: number;
    total_count?: number;
  }>>;
  coverage?: { matches: number; expected_matches: number | null; has_full_schedule: boolean; results_complete: boolean | null; basis: string };
  preparation?: { status: string; message: string; counts?: { players?: number; photos?: number } };
}
const componentNames = { roster: "球员阵容", schedule: "比赛赛程", standings: "积分榜", portraits: "球员照片" };
const componentStatuses: Record<string, string> = {
  missing: "等待收录", fresh: "已更新", stale: "待更新", running: "更新中", partial: "部分收录", failed: "更新未完成", unavailable: "来源暂无资料",
};
const results: Record<string, string> = { W: "胜", D: "平", L: "负" };
const numberOrDash = (value?: number) =>
  value === undefined || value === null ? "—" : value;

// 指针倾斜：直接写 style.transform，不走 state，所以鼠标划过不会触发渲染。
// 用 rAF 合并同一帧内的多次 pointermove；只在细指针设备上启用，触屏保持静止。
function useTilt<T extends HTMLElement>(max = 4.5) {
  const ref = useRef<T | null>(null);
  useEffect(() => {
    const node = ref.current;
    if (!node) return;
    if (typeof window.matchMedia !== "function") return;
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    if (!window.matchMedia("(hover: hover) and (pointer: fine)").matches) return;
    let frame = 0;
    let x = 0;
    let y = 0;
    let px = 50;
    let py = 0;
    const apply = () => {
      frame = 0;
      node.style.transform = `rotateX(${y.toFixed(2)}deg) rotateY(${x.toFixed(2)}deg)`;
      node.style.setProperty("--glare-x", `${px.toFixed(1)}%`);
      node.style.setProperty("--glare-y", `${py.toFixed(1)}%`);
    };
    const move = (event: PointerEvent) => {
      const box = node.getBoundingClientRect();
      if (!box.width || !box.height) return;
      const fx = (event.clientX - box.left) / box.width;
      const fy = (event.clientY - box.top) / box.height;
      // 以中心为原点归一化到 [-1, 1]，再乘上限角度
      x = (fx - 0.5) * 2 * max;
      y = -(fy - 0.5) * 2 * max;
      px = fx * 100;
      py = fy * 100;
      if (!frame) frame = requestAnimationFrame(apply);
    };
    const leave = () => {
      if (frame) cancelAnimationFrame(frame);
      frame = 0;
      node.style.transform = "";
      node.style.removeProperty("--glare-x");
      node.style.removeProperty("--glare-y");
    };
    node.addEventListener("pointermove", move);
    node.addEventListener("pointerleave", leave);
    return () => {
      if (frame) cancelAnimationFrame(frame);
      node.removeEventListener("pointermove", move);
      node.removeEventListener("pointerleave", leave);
      node.style.transform = "";
    };
  }, [max]);
  return ref;
}

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
  const [tab, setTab] = useState("overview");
  const [position, setPosition] = useState("");
  const [playerQuery, setPlayerQuery] = useState("");
  const [player, setPlayer] = useState<Player | null>(null);
  const [matchTab, setMatchTab] = useState("upcoming");
  const [matchLimit, setMatchLimit] = useState(8);
  const [revealedTeam, setRevealedTeam] = useState<string | null>(null);
  const reveal = revealedTeam === team.id;
  const heroRef = useTilt<HTMLElement>();
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
    setTab("overview");
    setPosition("");
    setPlayerQuery("");
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
  useEffect(() => {
    const refreshVisible = () => {
      if (document.visibilityState === "visible" && !reading.current) void load();
    };
    const preparing = center?.preparation?.status === "running" ||
      Object.values(center?.components || {}).some((item) => item?.status === "running");
    const timer = window.setInterval(refreshVisible, preparing ? 5000 : 60000);
    document.addEventListener("visibilitychange", refreshVisible);
    window.addEventListener("online", refreshVisible);
    return () => {
      clearInterval(timer);
      document.removeEventListener("visibilitychange", refreshVisible);
      window.removeEventListener("online", refreshVisible);
    };
  }, [load, center?.preparation?.status, center?.components]);
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
  return (
    <div className="team-center">
      <section className="club-hero" ref={heroRef}>
        <div className="club-hero-art" aria-hidden="true">
          <i />
          <b />
          <span />
        </div>
        <div className="club-hero-glare" aria-hidden="true" />
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
      <WorkflowControls kind="team" teamId={team.id} onChange={workflowChanged} />
      {center?.preparation?.status === "running" && (
        <div className="material-status" role="status"><Spinner /> 正在准备这支球队的名单、照片和比赛资料。</div>
      )}
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
      {center && (
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
      {center?.components && (
        <details className="team-data-status">
          <summary>资料更新情况<span>{Object.values(center.components).some((part) => part?.status === "running") ? "正在补充资料" : "查看各项来源状态"}</span></summary>
          <div className="data-component-grid">
            {(Object.keys(componentNames) as (keyof typeof componentNames)[]).map((key) => {
              const part = center.components?.[key];
              if (!part) return null;
              return <div key={key}>
                <strong>{componentNames[key]}</strong>
                <span className={`data-state data-state-${part.status}`}>{componentStatuses[part.status] || part.status}</span>
                <small>{key === "portraits" && part.total_count != null ? `${part.available_count || 0} / ${part.total_count} 张有来源照片` : part.updated_at ? `更新于${friendlyTime(part.updated_at)}` : "尚无有效快照"}</small>
                {!!part.errors?.length && <p>{part.errors.join("；")}</p>}
              </div>;
            })}
          </div>
        </details>
      )}
      <DecisionHistory teamId={team.id} version={[
        center?.updated_at, center?.components?.portraits?.updated_at, center?.refresh_status,
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
                  "暂无球队介绍，可以向助手了解球队的历史与风格。"}
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
                {center?.roster_season?.label || center?.season?.label || "当前赛季"} ·{" "}
                {center?.roster.length || 0} 名球员
              </p>
              {center?.roster_fallback && (
                <p>当前为 {center?.roster_season?.label} 赛季名单；{center?.latest_season?.label} 赛季球队目录尚未齐全。</p>
              )}
            </div>
            <Users size={21} />
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
                  <h3>
                    {item.name_zh || item.name}
                    {item.name_zh && <small className="player-name-source">{item.name}</small>}
                  </h3>
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
              <h3>{center?.roster.length ? "没有匹配的球员" : "还没有可读的阵容资料"}</h3>
              <p>{center?.roster.length ? "试试中文名、原名，或查看全部位置。" : "点击上方“启动”更新一次球队资料，已确认的球员会出现在这里。"}</p>
              {!!center?.roster.length && <button className="button" onClick={() => { setPosition(""); setPlayerQuery(""); }}>查看全部球员</button>}
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
                          timeZone: "Asia/Shanghai",
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
                  <div className="match-opponent-copy">
                    <strong>{findClub(match.opponent)?.name || match.opponent}</strong>
                    {match.competition && <small className="match-competition">{match.competition}</small>}
                  </div>
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
        <Modal title={player.name_zh || player.name} onClose={() => setPlayer(null)}>
          <div className="player-detail">
            <div className="player-detail-hero">
              <div className="player-detail-photo">
                {player.number ? (
                  <span className="player-number">{player.number}</span>
                ) : null}
                <PlayerPhoto player={player} allowRetry />
              </div>
              <div className="player-detail-intro">
                {player.name_zh && <p className="player-original-name">{player.name}</p>}
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
                ["中文名", player.name_zh],
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
                ...(player.name_zh && safeUrl(player.name_zh_source_url)
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
}

function playerPhotoUrl(player: Player) {
  if (!safeUrl(player.photo_source_url)) return null;
  if (player.photo_match?.startsWith("wikimedia_") &&
      (!player.photo_credit?.trim() || !player.photo_license?.trim() || !safeUrl(player.photo_file_page))) return null;
  return safeUrl(player.photo_url);
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
