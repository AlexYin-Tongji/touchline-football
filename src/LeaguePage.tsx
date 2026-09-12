import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ArrowRight, ArrowUpRight, CalendarDays, Check, Database, EyeOff, RefreshCw, Shield, Trophy } from "lucide-react";
import { api, errorText, post } from "./api";
import { Crest, dateTime, Empty, ErrorNotice, safeUrl, Spinner } from "./components";
import type { League } from "./types";

export interface LeagueData { leagues: League[]; available_seasons: { season_start_year: number; label: string }[] }
export function useLeagues(spoiler?: boolean) {
  const [data, setData] = useState<LeagueData>({ leagues: [], available_seasons: [] });
  const [season, setSeason] = useState<number | undefined>();
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const active = useRef(true);
  const serial = useRef(0);
  const refresh = useCallback(async () => {
    const requestId = ++serial.current;
    try {
      const result = await api<LeagueData>(`/leagues${season ? `?season_start_year=${season}` : ""}`);
      if (active.current && requestId === serial.current) { setData(result); setError(""); }
    } catch (failure) { if (active.current && requestId === serial.current) setError(errorText(failure)); }
    finally { if (active.current) setLoading(false); }
  }, [season]);
  useEffect(() => { active.current = true; setLoading(true); void refresh(); return () => { active.current = false; }; }, [refresh, spoiler]);
  const running = data.leagues.some(league => league.sync_status === "running");
  useEffect(() => { const timer = window.setInterval(() => { if (document.visibilityState === "visible") void refresh(); }, running ? 3000 : 60000); return () => window.clearInterval(timer); }, [refresh, running]);
  const visibleLeagues = useMemo(() => spoiler ? data.leagues.map(league => ({ ...league, standings: [], standings_hidden: true, fixtures: league.fixtures?.map(fixture => ({ ...fixture, home_score: undefined, away_score: undefined, spoiler_hidden: true })) })) : data.leagues, [data.leagues, spoiler]);
  return { ...data, leagues: visibleLeagues, season, setSeason, error, loading, refresh, running };
}

const leagueColors: Record<string, string> = { PL: "#562879", PD: "#aa4935", SA: "#2763a5", BL1: "#a83131", FL1: "#27644e" };
const statusLabel: Record<string, string> = { idle: "等待首次同步", running: "正在同步赛程与积分", completed: "同步已完成", partial: "已同步部分资料", failed: "本次同步未完成" };
const count = (league: League, key: string) => typeof league.coverage?.[key] === "number" ? Number(league.coverage[key]) : 0;

export function LeaguePage({ state, onTeam, onSettings }: { state: ReturnType<typeof useLeagues>; onTeam: (id: string) => void; onSettings: () => void }) {
  const [selected, setSelected] = useState("");
  const [tab, setTab] = useState("teams");
  const [busy, setBusy] = useState("");
  const [actionError, setActionError] = useState("");
  const [notice, setNotice] = useState("");
  const actionLock = useRef(false);
  const league = state.leagues.find(item => item.id === selected) || state.leagues[0];
  async function sync(id?: string) {
    if (actionLock.current) return;
    actionLock.current = true; setBusy(id || "all"); setActionError(""); setNotice("");
    try {
      await post(id ? `/leagues/${encodeURIComponent(id)}/sync` : "/leagues/sync", { season_start_year: state.season || league?.season_start_year });
      setNotice(id ? "正在同步所选联赛，进度会自动更新。" : "五大联赛已开始依次同步，进度会自动更新。");
      await state.refresh();
    } catch (error) { setActionError(errorText(error)); }
    finally { setBusy(""); actionLock.current = false; }
  }
  const total = state.leagues.reduce((sum, item) => sum + item.team_count, 0);
  const fixtures = [...(league?.fixtures || [])].sort((a, b) => {
    const aFuture = !["finished", "completed", "FT"].includes(a.status);
    const bFuture = !["finished", "completed", "FT"].includes(b.status);
    return Number(bFuture) - Number(aFuture) || (aFuture ? (a.kickoff || "~").localeCompare(b.kickoff || "~") : (b.kickoff || "").localeCompare(a.kickoff || ""));
  }).slice(0, 30);
  return <div className="league-page"><div className="hub-title-row"><div><span className="overline">足球资料库</span><h1>五大联赛，每一支球队。</h1><p className="muted">真实赛季资料 · 从球队档案到比赛日程，按已核实的联赛归属查看。</p></div><button className="button dark-button" disabled={!!busy || state.running || !state.leagues.length} onClick={() => sync()}><RefreshCw size={16} className={state.running ? "spin" : ""} />{state.running ? "联赛正在同步" : "同步五大联赛"}</button></div>
    <ErrorNotice>{state.error || actionError}</ErrorNotice>{state.error && <button className="button" onClick={() => state.refresh()}><RefreshCw size={15} />重新加载联赛资料</button>}{notice && <p className="league-sync-notice" role="status">{notice}</p>}
    <div className="league-overview"><div><Database size={23} /><span><strong>{state.leagues.length}</strong> 个联赛</span><span><strong>{total}</strong> 支球队</span></div><label>查看赛季<select aria-label="联赛赛季" value={state.season || league?.season_start_year || ""} onChange={event => state.setSeason(Number(event.target.value))}>{state.available_seasons.map(season => <option key={season.season_start_year} value={season.season_start_year}>{season.label}</option>)}</select></label></div>
    {state.loading && !state.leagues.length && <Spinner text="正在打开五大联赛资料库" />}
    <div className="league-selector" role="group" aria-label="选择联赛">{state.leagues.map(item => <button key={item.id} onClick={() => { setSelected(item.id); setTab("teams"); setNotice(""); }} aria-pressed={league?.id === item.id} className={league?.id === item.id ? "selected" : ""} style={{ "--league-color": leagueColors[item.id] || "#315c49" } as React.CSSProperties}><span className={`league-monogram ${item.logo_url ? "has-logo" : ""}`}>{item.logo_url?.startsWith("/assets/leagues/") ? <img src={item.logo_url} alt="" /> : item.name.slice(0, 2)}</span><strong>{item.name}</strong><span className="league-country">{item.country} · {item.season_label}</span><small>{item.team_count} / {item.expected_team_count} 支球队{item.team_count === item.expected_team_count && <Check size={13} />}</small></button>)}</div>
    {league && <section className="league-detail"><div className="league-detail-heading"><div><span className="overline">{league.season_label} 赛季</span><h2>{league.name}</h2><p>{statusLabel[league.sync_status || "idle"]}{league.last_sync && typeof league.last_sync === "string" ? ` · 最近同步 ${dateTime(league.last_sync)}` : ""}</p></div><div><button className="button" disabled={!!busy || league.sync_status === "running" || state.running} onClick={() => sync(league.id)}><RefreshCw size={15} className={league.sync_status === "running" ? "spin" : ""} />同步本联赛</button>{safeUrl(league.source_url) && <a href={safeUrl(league.source_url)!} target="_blank" rel="noopener noreferrer" className="text-button">数据来源<ArrowUpRight size={14} /></a>}</div></div>
      <div className="league-coverage"><div><Shield size={17} /><strong>{league.team_count} / {league.expected_team_count}</strong><span>球队档案</span></div><div><CalendarDays size={17} /><strong>{count(league, "matches")} / {count(league, "expected_matches") || "—"}</strong><span>{league.coverage?.has_full_schedule ? "完整赛程已收录" : "已收录比赛"}</span></div><div><Trophy size={17} /><strong>{league.standings_hidden ? "已隐藏" : `${count(league, "standings_rows")} / ${league.expected_team_count}`}</strong><span>积分榜球队</span></div></div>
      {!!league.errors?.length && <details className="league-errors"><summary>查看同步详情（{league.errors.length} 项）</summary><ul>{league.errors.map((error, index) => <li key={index}>{error}</li>)}</ul></details>}
      <div className="league-tabs" role="group" aria-label="联赛资料类型">{[["teams", "球队档案"], ["fixtures", "比赛日程"], ["standings", "积分榜"]].map(([id, label]) => <button className={tab === id ? "active" : ""} aria-pressed={tab === id} key={id} onClick={() => setTab(id)}>{label}</button>)}</div>
      {tab === "teams" ? <div className="league-team-grid">{league.teams.map(team => <button key={team.id} onClick={() => onTeam(team.id)}><Crest team={team} /><span><strong>{team.name}</strong><small>{team.english_name}</small></span><ArrowRight size={15} /></button>)}</div> : tab === "standings" ? league.standings_hidden ? <Empty title="积分榜已按避剧透偏好隐藏" action={<button className="button" onClick={onSettings}><EyeOff size={15} />调整避剧透设置</button>}>积分和名次可能透露最近的赛果。</Empty> : league.standings?.length ? <div className="league-table-scroll" role="region" aria-label={`${league.name}积分榜，可横向滚动`} tabIndex={0}><table className="league-standings"><caption>{league.name} {league.season_label} 积分榜 · {league.standings.length} 支已同步球队<span className="table-swipe-hint">左右滑动查看完整数据</span></caption><thead><tr><th scope="col">名次</th><th scope="col">球队</th><th scope="col">场</th><th scope="col">胜</th><th scope="col">平</th><th scope="col">负</th><th scope="col">净胜球</th><th scope="col">积分</th></tr></thead><tbody>{league.standings.map((row, index) => { const team = league.teams.find(team => team.id === row.team_id); return <tr key={row.team_id || index}><td>{row.rank ?? row.position ?? "—"}</td><th scope="row">{team ? <button onClick={() => onTeam(team.id)}><Crest team={team} small />{team.name}</button> : row.team_name || row.name || "球队资料暂缺"}</th><td>{row.played ?? "—"}</td><td>{row.won ?? "—"}</td><td>{row.drawn ?? "—"}</td><td>{row.lost ?? "—"}</td><td>{row.goal_difference ?? "—"}</td><td><strong>{row.points ?? "—"}</strong></td></tr>; })}</tbody></table></div> : <Empty title="积分榜还没有同步" action={<button className="button" disabled={state.running || !!busy} onClick={() => sync(league.id)}>同步{league.name}数据</button>}>连接公开数据源后，这里会显示实际积分与排名。</Empty> : fixtures.length ? <div className="league-fixtures"><p className="quiet-label">优先展示接下来的比赛，随后为最近赛果 · 北京时间 · 最多 30 场</p>{fixtures.map(fixture => <div className="league-fixture" key={fixture.id}><time>{fixture.kickoff_confirmed === false ? fixture.kickoff ? `${fixture.kickoff.slice(0, 10)} · 时间待定` : "时间待定" : dateTime(fixture.kickoff)}</time><strong>{league.teams.find(team => team.id === fixture.home_team_id)?.name || fixture.home_team}</strong><span className="league-fixture-score">{fixture.spoiler_hidden ? <EyeOff size={16} aria-label="赛果已隐藏" /> : fixture.home_score != null && fixture.away_score != null ? `${fixture.home_score} : ${fixture.away_score}` : "vs"}</span><strong>{league.teams.find(team => team.id === fixture.away_team_id)?.name || fixture.away_team}</strong>{safeUrl(fixture.source_url) && <a href={safeUrl(fixture.source_url)!} target="_blank" rel="noopener noreferrer" aria-label={`查看${fixture.home_team}对${fixture.away_team}比赛来源`}><ArrowUpRight size={15} /></a>}</div>)}</div> : <Empty title="比赛日程等待首次同步" action={<button className="button" disabled={state.running || !!busy} onClick={() => sync(league.id)}>同步{league.name}数据</button>}>球队档案已就绪。同步完成后会列出本赛季真实的对阵与开球时间。</Empty>}
    </section>}
  </div>;
}
