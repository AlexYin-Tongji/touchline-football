import {
  useState,
  type CSSProperties,
  type PointerEvent,
  type ReactNode,
} from "react";
import {
  ArrowRight,
  ArrowUpRight,
  Bookmark,
  CalendarDays,
  Check,
  ChevronRight,
  Clock3,
  EyeOff,
  HeartPulse,
  MapPin,
  Newspaper,
  RefreshCw,
  Shield,
  Sparkles,
  Trophy,
  Users,
} from "lucide-react";
import { Crest, dateTime, Empty, safeUrl, sourceLabel } from "./components";
import { findClub } from "./clubCatalog";
import type { AppState, ClubEvent, Evidence, Match, Run, Team } from "./types";

export const chineseText = (value = "", fallback = "球队消息") => {
  const cleaned = value
    .replace(/【[^】]*】/g, "")
    .replace(/\*\*/g, "")
    .trim();
  const chinese = (cleaned.match(/[\u3400-\u9fff]/g) || []).length;
  const latin = (cleaned.match(/[A-Za-z]/g) || []).length;
  return chinese > 0 &&
    latin <= Math.max(20, chinese) &&
    !/(?:[A-Za-z]+[\s,;:]+){5}[A-Za-z]+/.test(cleaned)
    ? cleaned
    : fallback;
};
const categories: Record<string, string> = {
  team: "球队动态",
  injury: "伤停消息",
  transfer: "转会进展",
  match: "比赛消息",
};
const teamNames: Record<string, string> = {
  "al ahly": "开罗国民",
  alaves: "阿拉维斯",
  "athletic club": "毕尔巴鄂竞技",

  bayern: "拜仁慕尼黑",
  "birmingham city": "伯明翰",
  "borussia dortmund": "多特蒙德",
  "celta de vigo": "维戈塞尔塔",
  "como 1907": "科莫",
  "deportivo de la coruna": "拉科鲁尼亚",
  elche: "埃尔切",
  espanyol: "西班牙人",
  "basel": "巴塞尔",
  feyenoord: "费耶诺德",
  galatasaray: "加拉塔萨雷",
  getafe: "赫塔费",
  girona: "赫罗纳",
  levante: "莱万特",
  "lille osc": "里尔",
  malaga: "马拉加",
  osasuna: "奥萨苏纳",
  "paris saint germain": "巴黎圣日耳曼",
  "racing de santander": "桑坦德竞技",
  "rayo vallecano": "巴列卡诺",
  "real betis": "皇家贝蒂斯",
  "real sociedad": "皇家社会",
  sevilla: "塞维利亚",
  "sporting lisbon": "葡萄牙体育",
  "valencia cf": "瓦伦西亚",
  villarreal: "比利亚雷亚尔",
  arsenal: "阿森纳",
  liverpool: "利物浦",
  barcelona: "巴塞罗那",
  inter: "国际米兰",
  "inter milan": "国际米兰",
  chelsea: "切尔西",
  "manchester city": "曼城",
  "manchester united": "曼联",
  tottenham: "热刺",
  "tottenham hotspur": "热刺",
  "real madrid": "皇家马德里",
  "atletico madrid": "马德里竞技",
  everton: "埃弗顿",
  "aston villa": "阿斯顿维拉",
  "newcastle united": "纽卡斯尔联",
  "nottingham forest": "诺丁汉森林",
  fulham: "富勒姆",
  "crystal palace": "水晶宫",
  "west ham united": "西汉姆联",
  brighton: "布莱顿",
  "brighton and hove albion": "布莱顿",
  brentford: "布伦特福德",
  bournemouth: "伯恩茅斯",
  "leeds united": "利兹联",
  sunderland: "桑德兰",
  burnley: "伯恩利",
  juventus: "尤文图斯",
  milan: "AC米兰",
  "ac milan": "AC米兰",
  napoli: "那不勒斯",
  roma: "罗马",
  lazio: "拉齐奥",
  atalanta: "亚特兰大",
  "bayern munich": "拜仁慕尼黑",
  "paris saint-germain": "巴黎圣日耳曼",
};
export const clubName = (value: string) =>
  findClub(value)?.name ||
  teamNames[
    value
      .normalize("NFD")
      .replace(/\p{Diacritic}/gu, "")
      .toLowerCase()
      .replace(/\bfc\b/g, "")
      .replace(/&/g, "and")
      .trim()
  ] ||
  chineseText(value, value || "对手待定");
const competitionName = (value = "") =>
  /premier league/i.test(value)
    ? "英超联赛"
    : /champions league/i.test(value)
      ? "欧冠联赛"
      : /la liga|laliga/i.test(value)
        ? "西甲联赛"
        : /serie a/i.test(value)
          ? "意甲联赛"
          : /fa cup/i.test(value)
            ? "足总杯"
            : /friendly/i.test(value)
              ? "友谊赛"
              : chineseText(value, "赛事待确认");
const timeValue = (value?: string | null) =>
  value && Number.isFinite(Date.parse(value)) ? Date.parse(value) : 0;
const shortDate = (value?: string) =>
  timeValue(value)
    ? new Date(value!).toLocaleDateString("zh-CN", {
        month: "long",
        day: "numeric",
        timeZone: "Asia/Shanghai",
      })
    : "日期待定";

function Tilt({
  children,
  className = "",
  style,
}: {
  children: ReactNode;
  className?: string;
  style?: CSSProperties;
}) {
  function move(e: PointerEvent<HTMLDivElement>) {
    if (
      e.pointerType !== "mouse" ||
      window.matchMedia("(prefers-reduced-motion: reduce)").matches
    )
      return;
    const rect = e.currentTarget.getBoundingClientRect();
    e.currentTarget.style.setProperty(
      "--tilt-x",
      `${(0.5 - (e.clientY - rect.top) / rect.height) * 5}deg`,
    );
    e.currentTarget.style.setProperty(
      "--tilt-y",
      `${((e.clientX - rect.left) / rect.width - 0.5) * 5}deg`,
    );
  }
  return (
    <div
      className={`tilt-surface ${className}`}
      style={style}
      onPointerMove={move}
      onPointerLeave={(e) => {
        e.currentTarget.style.setProperty("--tilt-x", "0deg");
        e.currentTarget.style.setProperty("--tilt-y", "0deg");
      }}
    >
      {children}
    </div>
  );
}

function Opponent({ name, teams }: { name: string; teams: Team[] }) {
  const found =
    teams.find(
      (t) =>
        t.name === name || t.english_name?.toLowerCase() === name.toLowerCase(),
    ) || findClub(name);
  return found ? (
    <Crest team={found} />
  ) : (
    <span className="opponent-crest" aria-label="对手">
      <Shield size={30} strokeWidth={1.35} />
    </span>
  );
}

function MatchCard({
  match,
  team,
  teams,
  onAsk,
}: {
  match?: Match;
  team: Team;
  teams: Team[];
  onAsk: (question: string, match?: Match) => void;
}) {
  return (
    <section className="hub-card next-game">
      <div className="hub-section-heading">
        <h2>
          <CalendarDays size={18} />
          下一场比赛
        </h2>
        {match && (
          <span className="quiet-label">
            {match.venue === "home"
              ? "主场"
              : match.venue === "away"
                ? "客场"
                : "场地待定"}
          </span>
        )}
      </div>
      {match ? (
        <>
          <span className="match-competition">
            {competitionName(match.competition)}
          </span>
          <div className="hub-contest">
            <div>
              <Crest team={team} />
              <strong>{team.name}</strong>
            </div>
            <span className="versus">对阵</span>
            <div>
              <Opponent name={match.opponent} teams={teams} />
              <strong>{clubName(match.opponent)}</strong>
            </div>
          </div>
          <div className="kickoff-line">
            <Clock3 size={15} />
            {!match.kickoff || match.kickoff_confirmed === false
              ? `${shortDate(match.kickoff_date || match.kickoff)} · 时间待定`
              : match.kickoff
                ? dateTime(match.kickoff)
                : "开球时间待定"}
          </div>
          <button
            className="button match-ask"
            onClick={() =>
              onAsk(
                `分析${team.name}对阵${match.opponent}的赛前看点、伤停与可能影响。`,
                match,
              )
            }
          >
            聊聊这场比赛
            <ArrowUpRight size={16} />
          </button>
        </>
      ) : (
        <div className="hub-empty">
          <CalendarDays size={32} />
          <h3>等待下一场赛程</h3>
          <p>更新动态后，已公布的赛程会出现在这里。</p>
        </div>
      )}
    </section>
  );
}

function RecentForm({
  matches,
  spoiler,
  onSettings,
}: {
  matches: Match[];
  spoiler: boolean;
  onSettings: () => void;
}) {
  const recent = matches
    .filter((x) => x.status === "finished")
    .sort((a, b) => timeValue(b.kickoff) - timeValue(a.kickoff))
    .slice(0, 5);
  const scored = recent.filter(
    (x) => Number.isFinite(x.goals_for) && Number.isFinite(x.goals_against),
  );
  return (
    <section className="hub-card form-card">
      <div className="hub-section-heading">
        <h2>
          <Trophy size={18} />
          近期表现
        </h2>
        <span className="quiet-label">最近五场</span>
      </div>
      {spoiler ? (
        <div className="form-private">
          <EyeOff size={23} />
          <p>赛果已为你隐藏</p>
          <button className="text-button" onClick={onSettings}>
            调整避剧透设置
            <ChevronRight size={14} />
          </button>
        </div>
      ) : recent.length ? (
        <>
          <div className="form-results">
            {recent.map((m) => {
              const result =
                m.result ||
                (Number.isFinite(m.goals_for) &&
                Number.isFinite(m.goals_against)
                  ? m.goals_for! > m.goals_against!
                    ? "W"
                    : m.goals_for === m.goals_against
                      ? "D"
                      : "L"
                  : "");
              return (
                <span
                  key={m.id}
                  className={`result-badge ${result}`}
                  title={`${shortDate(m.kickoff)} · ${clubName(m.opponent)} · ${m.score || "比分暂缺"}`}
                >
                  {({ W: "胜", D: "平", L: "负" } as Record<string, string>)[
                    result
                  ] || "—"}
                </span>
              );
            })}
          </div>
          <div className="form-totals">
            <div>
              <strong>
                {scored.length
                  ? scored.reduce((s, x) => s + x.goals_for!, 0)
                  : "—"}
              </strong>
              <span>进球</span>
            </div>
            <div>
              <strong>
                {scored.length
                  ? scored.reduce((s, x) => s + x.goals_against!, 0)
                  : "—"}
              </strong>
              <span>失球</span>
            </div>
            <div>
              <strong>{recent.length}</strong>
              <span>场已收录</span>
            </div>
          </div>
          {scored.length < recent.length && (
            <p className="quiet-label">
              进失球统计基于 {scored.length} 场完整比分。
            </p>
          )}
        </>
      ) : (
        <div className="hub-empty compact">
          <Trophy size={27} />
          <p>还没有收录完场比赛</p>
        </div>
      )}
    </section>
  );
}

export function TeamHub({
  data,
  team,
  busy,
  onUpdate,
  onAsk,
  onEvent,
  onWatch,
  onSettings,
  onProfile,
  onRun,
}: {
  data: AppState;
  team: Team;
  busy: boolean;
  onUpdate: () => void;
  onAsk: (question: string, match?: Match) => void;
  onEvent: (event: ClubEvent) => void;
  onWatch: (event: ClubEvent) => void;
  onSettings: () => void;
  onProfile: () => void;
  onRun: (run: Run) => void;
}) {
  const [category, setCategory] = useState("all");
  const [shown, setShown] = useState(6);
  const events = data.events
    .filter((x) => x.team_id === team.id)
    .sort((a, b) => timeValue(b.updated_at) - timeValue(a.updated_at));
  const evidence = data.evidence.filter(
    (x) => x.team_id === team.id && !x.spoiler_hidden,
  );
  const matches = data.matches.filter((x) => x.team_id === team.id);
  const today = new Date().toLocaleDateString("en-CA", {
    timeZone: "Asia/Shanghai",
  });
  const upcoming = matches
    .filter(
      (x) =>
        x.status !== "finished" &&
        !["cancelled", "postponed", "live"].includes(x.fixture_status || "") &&
        (x.kickoff
          ? timeValue(x.kickoff) > Date.now()
          : !x.kickoff_date || x.kickoff_date >= today),
    )
    .sort(
      (a, b) =>
        (timeValue(a.kickoff || a.kickoff_date) || Infinity) -
        (timeValue(b.kickoff || b.kickoff_date) || Infinity),
    );
  const visibleEvents = events.filter(
    (x) =>
      category === "all" ||
      (category === "watching" ? x.watching : x.kind === category),
  );
  const brief = data.runs
    .filter(
      (x) =>
        x.team_id === team.id &&
        x.skill === "team_brief" &&
        x.status === "completed",
    )
    .sort((a, b) => timeValue(b.finished_at) - timeValue(a.finished_at))[0];
  const latest = evidence
    .map((x) => x.fetched_at)
    .filter(Boolean)
    .sort()
    .at(-1);
  const sourceCount = new Set(
    evidence.map((x) => x.source_name).filter(Boolean),
  ).size;
  const summary = brief?.structured_answer?.summary;
  return (
    <div className="hub-view">
      <div className="hub-title-row">
        <div>
          <span className="overline">我的主场</span>
          <h1>每个变化，都跟得上。</h1>
        </div>
        <button
          className="button dark-button"
          disabled={busy}
          onClick={onUpdate}
        >
          <RefreshCw size={17} className={busy ? "spin" : ""} />
          {busy ? "正在更新" : "更新动态"}
        </button>
      </div>
      <Tilt
        className="club-hero"
        style={{ "--club-color": team.color || "#19785b" } as CSSProperties}
      >
        <div className="hero-copy">
          <span className="hero-league">
            <span />
            {team.league_name || team.league || team.country || "我的球队"}
          </span>
          <h2>{team.name}</h2>
          <p>
            {team.nickname ? `${team.nickname} · ` : ""}
            {team.city || "属于你的足球主场"}
            {team.founded ? ` · 始于 ${team.founded} 年` : ""}
          </p>
          <div className="hero-actions">
            <button
              onClick={() => onAsk(`总结${team.name}最近值得关注的变化。`)}
            >
              听听助手怎么说
              <ArrowUpRight size={17} />
            </button>
            {safeUrl(team.official_url) && (
              <a
                href={safeUrl(team.official_url) || undefined}
                target="_blank"
                rel="noopener noreferrer"
              >
                球队官网
                <ArrowUpRight size={15} />
              </a>
            )}
          </div>
        </div>
        <div className="hero-scene" aria-hidden="true">
          <div className="stadium-halo" />
          <div className="pitch-plane">
            <div className="pitch-boundary">
              <div className="pitch-half" />
              <div className="pitch-center" />
              <div className="pitch-box top" />
              <div className="pitch-box bottom" />
            </div>
          </div>
          <div className="hero-badge">
            <Crest team={team} />
          </div>
          <div className="crest-shadow" />
        </div>
        <div className="hero-bottom">
          <span>
            <MapPin size={15} />
            {team.stadium || team.city || "球队主场资料待补充"}
          </span>
          <span>
            {latest ? `最近收录 ${shortDate(latest)}` : "开启你的跟队日常"}
          </span>
        </div>
      </Tilt>
      <div className="hub-grid">
        <div className="hub-main-column">
          {summary && (
            <section className="brief-ribbon">
              <span className="brief-icon">
                <Sparkles size={21} />
              </span>
              <div>
                <span>助手简报 · {shortDate(brief.finished_at || brief.created_at)}</span>
                <p>{chineseText(summary, "球队简报已整理完成。")}</p>
              </div>
              <button
                className="icon-button"
                aria-label="阅读完整简报"
                onClick={() => onRun(brief)}
              >
                <ArrowUpRight size={20} />
              </button>
            </section>
          )}
          <section className="hub-card news-card">
            <div className="hub-section-heading">
              <h2>
                <Newspaper size={19} />
                球队动态
              </h2>
              <span className="quiet-label">{events.length} 条消息</span>
            </div>
            <div className="news-filters" aria-label="筛选球队动态">
              {[
                ["all", "全部"],
                ["team", "球队"],
                ["injury", "伤停"],
                ["transfer", "转会"],
                ["match", "比赛"],
                ["watching", "已关注"],
              ].map(([id, label]) => (
                <button
                  key={id}
                  aria-pressed={category === id}
                  className={category === id ? "active" : ""}
                  onClick={() => {
                    setCategory(id);
                    setShown(6);
                  }}
                >
                  {label}
                </button>
              ))}
            </div>
            <div className="hub-news-list">
              {visibleEvents.slice(0, shown).map((event) => {
                const categoryLabel =
                  categories[event.kind || "team"] || "球队动态";
                const sources = event.evidence_ids
                  .map((id) => evidence.find((e) => e.id === id))
                  .filter((e): e is Evidence => !!e);
                const source = sources.find((s) => safeUrl(s.url));
                return (
                  <article className="hub-news-item" key={event.id}>
                    <div className="news-meta">
                      <span className={`news-category ${event.kind || "team"}`}>
                        {categoryLabel}
                      </span>
                      <time>{shortDate(event.updated_at)}</time>
                      {event.display_status === "unverified" &&
                        data.settings.mode === "live" && (
                          <span className="news-pending">待确认</span>
                        )}
                      {!event.read && (
                        <span className="unread-dot" title="未读" />
                      )}
                    </div>
                    <button
                      className="news-title"
                      onClick={() => onEvent(event)}
                    >
                      <h3>
                        {chineseText(
                          event.display_title || event.title,
                          `${categoryLabel}有新消息`,
                        )}
                      </h3>
                      <ChevronRight size={18} />
                    </button>
                    <p>
                      {chineseText(
                        event.display_summary || event.summary || event.change,
                        "已收录最新消息，可请助手整理中文要点。",
                      ).slice(0, 180)}
                    </p>
                    <div className="news-item-footer">
                      {source ? (
                        <a
                          href={safeUrl(source.url) || undefined}
                          target="_blank"
                          rel="noopener noreferrer"
                        >
                          {sourceLabel(source)}
                          <ArrowUpRight size={13} />
                        </a>
                      ) : (
                        <span className="quiet-label">
                          {data.settings.mode === "demo"
                            ? "演示资料"
                            : "本地资料"}
                        </span>
                      )}
                      <div>
                        <button
                          onClick={() =>
                            onAsk(
                              `解读“${event.display_title || chineseText(event.title)}”，说明已确认的情况与对${team.name}的影响。`,
                            )
                          }
                        >
                          <Sparkles size={14} />
                          问助手
                        </button>
                        <button
                          aria-label={
                            event.watching ? "取消关注这条动态" : "关注这条动态"
                          }
                          aria-pressed={!!event.watching}
                          onClick={() => onWatch(event)}
                        >
                          <Bookmark
                            size={15}
                            fill={event.watching ? "currentColor" : "none"}
                          />
                          {event.watching ? "已关注" : "关注"}
                        </button>
                      </div>
                    </div>
                  </article>
                );
              })}
            </div>
            {!visibleEvents.length && (
              <Empty
                title={
                  category === "all"
                    ? "球队的故事，从这里开始"
                    : "暂时没有这类动态"
                }
              >
                {category === "all"
                  ? "点击“更新动态”收集消息，或直接向助手提问。"
                  : "有新消息时会出现在这里。"}
              </Empty>
            )}
            {visibleEvents.length > shown && (
              <button className="more-news" onClick={() => setShown(shown + 6)}>
                展开更多动态
                <ChevronRight size={16} />
              </button>
            )}
          </section>
          <section className="hub-card schedule-card">
            <div className="hub-section-heading">
              <h2>
                <CalendarDays size={18} />
                比赛日历
              </h2>
              <span className="quiet-label">北京时间</span>
            </div>
            {matches.length ? (
              <div className="schedule-list">
                {[...matches]
                  .sort((a, b) => {
                    const futureA = a.status !== "finished",
                      futureB = b.status !== "finished";
                    return (
                      Number(futureB) - Number(futureA) ||
                      (futureA
                        ? (timeValue(a.kickoff || a.kickoff_date) || Infinity) -
                          (timeValue(b.kickoff || b.kickoff_date) || Infinity)
                        : timeValue(b.kickoff) - timeValue(a.kickoff))
                    );
                  })
                  .slice(0, 6)
                  .map((match) => (
                    <button
                      className="schedule-row"
                      key={match.id}
                      onClick={() =>
                        onAsk(
                          match.status === "finished"
                            ? `复盘${team.name}对阵${match.opponent}的这场比赛。`
                            : `介绍${team.name}对阵${match.opponent}的比赛看点。`,
                          match,
                        )
                      }
                    >
                      <time>
                        {shortDate(match.kickoff || match.kickoff_date)}
                      </time>
                      <span>
                        {match.venue === "away"
                          ? "客场"
                          : match.venue === "home"
                            ? "主场"
                            : "—"}
                      </span>
                      <strong>{clubName(match.opponent)}</strong>
                      <span className="schedule-score">
                        {match.status === "finished" ? (
                          data.settings.spoiler_free || match.spoiler_hidden ? (
                            <EyeOff size={15} />
                          ) : (
                            match.score || "已完场"
                          )
                        ) : (
                          (
                            {
                              postponed: "已延期",
                              cancelled: "已取消",
                              live: "进行中",
                            } as Record<string, string>
                          )[match.fixture_status || ""] || "未开赛"
                        )}
                      </span>
                      <ChevronRight size={16} />
                    </button>
                  ))}
              </div>
            ) : (
              <p className="inline-empty">还没有赛程，更新动态后自动补充。</p>
            )}
          </section>
        </div>
        <aside className="hub-side-column">
          <MatchCard
            match={upcoming[0]}
            team={team}
            teams={data.teams}
            onAsk={onAsk}
          />
          <RecentForm
            matches={matches}
            spoiler={data.settings.spoiler_free}
            onSettings={onSettings}
          />
          <section className="hub-card club-profile">
            <div className="hub-section-heading">
              <h2>
                <Users size={18} />
                认识球队
              </h2>
              <button className="text-button profile-edit" onClick={onProfile}>
                编辑
              </button>
            </div>
            <p>
              {team.description ||
                "球队介绍还未补充。你可以在资料设置中添加相关来源。"}
            </p>
            <dl>
              {[
                ["所在城市", team.city],
                ["成立年份", team.founded ? `${team.founded} 年` : undefined],
                ["主场", team.stadium],
              ]
                .filter(([, value]) => value)
                .map(([label, value]) => (
                  <div key={label}>
                    <dt>{label}</dt>
                    <dd>{value}</dd>
                  </div>
                ))}
            </dl>
            <div className="profile-sources">
              {team.profile_sources?.slice(0, 2).map(
                (source) =>
                  safeUrl(source.url) && (
                    <a
                      key={source.url}
                      href={safeUrl(source.url) || undefined}
                      target="_blank"
                      rel="noopener noreferrer"
                    >
                      {chineseText(source.name, "官方资料")}
                      <ArrowUpRight size={12} />
                    </a>
                  ),
              )}
            </div>
          </section>
          <div className="collection-note">
            <Check size={15} />
            <span>
              已收录 {evidence.length} 份资料
              {sourceCount ? ` · ${sourceCount} 个来源` : ""}
            </span>
          </div>
        </aside>
      </div>
    </div>
  );
}
