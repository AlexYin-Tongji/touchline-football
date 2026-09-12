import { useState } from "react";
import {
  ArrowRight,
  ArrowUpRight,
  BarChart3,
  Bell,
  BookOpen,
  CalendarDays,
  Check,
  ChevronDown,
  CircleAlert,
  Clock3,
  Download,
  EyeOff,
  HeartPulse,
  Monitor,
  RefreshCw,
  SearchCheck,
  ShieldCheck,
  Sparkles,
  Trophy,
  X,
} from "lucide-react";
import { post, put, errorText } from "./api";
import {
  Crest,
  dateTime,
  Empty,
  ErrorNotice,
  safeUrl,
  Spinner,
} from "./components";
import type {
  AppState,
  ClubEvent,
  Evidence,
  Match,
  Mode,
  Recommendation,
  Run,
  Team,
} from "./types";

const compactText = (value: string, length = 130) =>
  value
    .replace(/【[^】]*】/g, "")
    .replace(/\[[^\]]+\]/g, "")
    .replace(/[*#]/g, "")
    .trim()
    .slice(0, length) + (value.length > length ? "…" : "");
const isChinese = (value = "") => /[\u3400-\u9fff]/.test(value);
const chartDate = (value?: string) =>
  value
    ? new Date(value).toLocaleDateString("zh-CN", {
        year: "numeric",
        month: "numeric",
        day: "numeric",
        timeZone: "Asia/Shanghai",
      })
    : "日期未知";
const normalizeClaim = (value: string) =>
  value.normalize("NFKC").replace(/\s+/g, "");
const eventLabels: Record<string, string> = {
  injury: "伤情与出场",
  transfer: "转会进展",
  match: "比赛消息",
  team: "球队动态",
};

export function CompetitionMark({ name }: { name?: string }) {
  const premier = /英超|premier league/i.test(name || "");
  const [failed, setFailed] = useState(false);
  return (
    <span className="competition-mark">
      {premier && !failed ? (
        <img
          src="/assets/premier-league.png"
          alt="英超标识"
          onError={() => setFailed(true)}
        />
      ) : (
        <Trophy size={15} />
      )}
      <span>{name || "赛事待确认"}</span>
    </span>
  );
}

export function TodayPage({
  data,
  team,
  mode,
  brief,
  active,
  busy,
  onUpdate,
  onBrief,
  onSources,
  onSettings,
  onMatch,
  onEvent,
  onEvidence,
  onRun,
  onRecommendation,
  onDismiss,
}: {
  data: AppState;
  team?: Team;
  mode: Mode;
  brief?: Run | null;
  active: boolean;
  busy: boolean;
  onUpdate: () => void;
  onBrief: () => void;
  onSources: () => void;
  onSettings: () => void;
  onMatch: (match?: Match) => void;
  onEvent: (event: ClubEvent) => void;
  onEvidence: (e: Evidence) => void;
  onRun: (r: Run) => void;
  onRecommendation: (r: Recommendation) => void;
  onDismiss: (r: Recommendation) => void;
}) {
  const events = data.events
    .filter((x) => x.team_id === team?.id && x.mode === mode)
    .sort(
      (a, b) =>
        Number(b.importance === "high") - Number(a.importance === "high") ||
        (b.updated_at || "").localeCompare(a.updated_at || ""),
    );
  const evidence = data.evidence.filter(
    (x) => x.team_id === team?.id && x.mode === mode && !x.spoiler_hidden,
  );
  const newsEvidence = evidence.filter((x) => !x.fixture_record);
  const next = data.matches
    .filter(
      (x) =>
        x.team_id === team?.id &&
        x.mode === mode &&
        x.status !== "finished" &&
        !["cancelled", "postponed", "live"].includes(x.fixture_status || "") &&
        (!x.kickoff || Date.parse(x.kickoff) > Date.now()),
    )
    .sort((a, b) =>
      (a.kickoff || "9999").localeCompare(b.kickoff || "9999"),
    )[0];
  const recommendations = (data.recommendations || [])
    .filter(
      (x) =>
        x.team_id === team?.id &&
        x.mode === mode &&
        !x.dismissed &&
        (!x.expires_at || Date.parse(x.expires_at) > Date.now()),
    )
    .slice(0, 3);
  const legacyFacts = brief?.answer
    ? Array.from(brief.answer.matchAll(/\*\*([^*\n]+)\*\*\s*\n([^\n]+)/g)).map(
        (match) => ({ title: match[1], detail: match[2] }),
      )
    : [];
  const facts = (
    brief?.status === "completed"
      ? brief.structured_answer?.facts || legacyFacts
      : []
  ).slice(0, 3);
  const latest = [
    data.automation_status?.last_checked,
    ...newsEvidence.map((x) => x.fetched_at),
  ]
    .filter(Boolean)
    .sort()
    .at(-1);
  const newer = brief?.finished_at
    ? newsEvidence.filter(
        (x) => x.fetched_at && x.fetched_at > brief.finished_at!,
      ).length
    : newsEvidence.length;
  const cards = facts.length
    ? facts.map((fact, i) => {
        const text =
          typeof fact === "string"
            ? fact
            : fact.detail || ("text" in fact ? fact.text : "") || "";
        const claimText =
          typeof fact === "string" ? fact : `${fact.title || ""}：${text}`;
        const claim = brief?.verification?.claims?.find(
          (c) => normalizeClaim(c.text) === normalizeClaim(claimText),
        );
        return {
          id: `fact-${i}`,
          title:
            typeof fact === "string"
              ? `值得知道的变化 ${i + 1}`
              : fact.title || compactText(text, 24),
          text,
          icon: <SearchCheck size={19} />,
          status:
            mode === "demo"
              ? "虚构示例"
              : !brief?.verification
                ? "历史答复 · 未做 V3 核对"
                : claim?.verdict === "supported"
                  ? "原文支持"
                  : claim?.verdict === "conflicting"
                    ? "来源冲突"
                    : "待补证据",
          time: brief?.finished_at,
          open: () => brief && onRun(brief),
        };
      })
    : events.slice(0, 3).map((event) => ({
        id: event.id,
        title: isChinese(event.title)
          ? compactText(event.title, 36)
          : `${eventLabels[event.kind || "team"] || "球队消息"}有新资料`,
        text: isChinese(event.summary || event.change)
          ? compactText(event.summary || event.change || "")
          : "已收录来源原文，具体说法和影响尚待解读。打开资料可核对出处与发布时间。",
        icon:
          event.kind === "injury" ? (
            <HeartPulse size={19} />
          ) : (
            <BookOpen size={19} />
          ),
        status: mode === "demo" ? "虚构示例" : "原文待核对",
        time: event.updated_at,
        open: () => onEvent(event),
      }));
  return (
    <div className="today-v3">
      <header className="today-heading">
        <div>
          <span className="eyebrow">YOUR CLUB, AT A GLANCE</span>
          <h1>今天，跟上{team?.name || "球队"}。</h1>
          <p>
            <Clock3 size={14} />
            {latest ? `资料更新于 ${dateTime(latest)}` : "还没有更新记录"}
            {newer > 0 && (
              <span className="new-count">{newer} 份待解读资料</span>
            )}
          </p>
        </div>
        <button
          className="button primary"
          onClick={onUpdate}
          disabled={busy || !!data.automation_status?.running}
        >
          {busy || data.automation_status?.running ? (
            <Spinner text="正在更新" />
          ) : (
            <>
              <RefreshCw size={16} />
              更新资料
            </>
          )}
        </button>
      </header>
      <div className="today-grid">
        <section className="today-changes">
          <div className="v3-section-title">
            <div>
              <span className="section-index">01</span>
              <h2>值得知道的变化</h2>
            </div>
            <button className="text-button" onClick={onSources}>
              全部资料
              <ArrowRight size={14} />
            </button>
          </div>
          {!cards.length ? (
            <div className="today-empty">
              <BookOpen size={28} />
              <h3>
                {evidence.length
                  ? "资料已就绪，开始一次解读"
                  : "从第一份球队消息开始"}
              </h3>
              <p>
                {evidence.length
                  ? `已收录 ${evidence.length} 份资料。解读后，这里只呈现最重要的三个变化。`
                  : "更新球队公开消息和赛程，再根据原文解释它们意味着什么。"}
              </p>
              <button
                className="button"
                onClick={evidence.length ? onBrief : onUpdate}
                disabled={busy || active}
              >
                <Sparkles size={15} />
                {evidence.length ? "解读最新消息" : "查找球队资料"}
              </button>
            </div>
          ) : (
            <div className="change-list">
              {cards.map((card) => (
                <article key={card.id} className="change-item">
                  <span className="change-icon">{card.icon}</span>
                  <div>
                    <button className="change-title" onClick={card.open}>
                      <h3>{card.title}</h3>
                      <ArrowUpRight size={17} />
                    </button>
                    <p>{compactText(card.text)}</p>
                    <button className="evidence-state" onClick={card.open}>
                      <SearchCheck size={13} />
                      {card.status}
                      <ArrowRight size={12} />
                    </button>
                    <time className="change-time">{dateTime(card.time)}</time>
                  </div>
                </article>
              ))}
            </div>
          )}
          {!!cards.length && (
            <footer className="change-footer">
              <span>
                {newer
                  ? "有新资料时，可重新解读变化。"
                  : "内容基于已收集资料；结论可查看原文。"}
              </span>
              <button
                className="text-button"
                onClick={onBrief}
                disabled={active || busy}
              >
                <Sparkles size={14} />
                {active ? "解读进行中" : "更新解读"}
              </button>
            </footer>
          )}
          {brief &&
            (brief.status === "running" ||
              brief.status === "queued" ||
              brief.status === "failed") && (
              <button className="brief-progress" onClick={() => onRun(brief)}>
                {active ? (
                  <Spinner text="助理正在核对资料" />
                ) : (
                  <>
                    <CircleAlert size={16} />
                    上次解读未完成
                  </>
                )}
                <span>
                  查看进度
                  <ArrowRight size={14} />
                </span>
              </button>
            )}
        </section>
        <section className="today-match">
          <div className="v3-section-title">
            <div>
              <span className="section-index">02</span>
              <h2>下一场比赛</h2>
            </div>
            <CalendarDays size={18} />
          </div>
          {next ? (
            <>
              <CompetitionMark name={next.competition} />
              <div className="today-contest">
                <div>
                  <Crest team={team} />
                  <strong>{team?.name}</strong>
                </div>
                <span>VS</span>
                <div>
                  <Crest name={next.opponent} />
                  <strong>{next.opponent}</strong>
                </div>
              </div>
              <p className="today-kickoff">
                {next.kickoff
                  ? dateTime(next.kickoff)
                  : next.kickoff_date || "时间待确认"}
              </p>
              <p className="today-match-meta">
                {next.kickoff ? "北京时间 · " : ""}
                {next.venue === "home"
                  ? "主场"
                  : next.venue === "away"
                    ? "客场"
                    : "场地待确认"}
                {next.kickoff_confirmed === false && " · 开球时间待确认"}
              </p>
              <button className="button primary" onClick={() => onMatch(next)}>
                查看比赛与看点
                <ArrowRight size={15} />
              </button>
              <p className="today-source">
                {mode === "demo"
                  ? "虚构示例赛程"
                  : next.source_name || "手动资料"}
                {next.synced_at && ` · ${dateTime(next.synced_at)}`}
              </p>
            </>
          ) : (
            <div className="today-empty">
              <CalendarDays size={30} />
              <h3>还没有下一场的可靠赛程</h3>
              <p>更新球队资料后显示赛程；未确认的时间会单独标注。</p>
              <button className="text-button" onClick={() => onMatch()}>
                前往比赛中心
                <ArrowRight size={14} />
              </button>
            </div>
          )}
        </section>
        <section className="today-recommendations">
          <div className="v3-section-title">
            <div>
              <span className="section-index">03</span>
              <h2>接下来，值得做什么</h2>
            </div>
            <span className="recommendation-hint">根据现有资料推荐</span>
          </div>
          {recommendations.length ? (
            <div className="recommendation-grid">
              {recommendations.map((r) => {
                const Icon =
                  r.kind === "follow"
                    ? Bell
                    : r.kind === "verify"
                      ? SearchCheck
                      : r.kind === "read"
                        ? BookOpen
                        : CalendarDays;
                const refs = r.evidence_ids
                  .map((id) => evidence.find((x) => x.id === id))
                  .filter((x): x is Evidence => !!x);
                return (
                  <article className="recommendation-card" key={r.id}>
                    <div className="recommendation-top">
                      <Icon size={20} />
                      <button
                        className="icon-button"
                        aria-label={`忽略推荐：${r.title}`}
                        title="忽略此推荐"
                        onClick={() => onDismiss(r)}
                      >
                        <X size={15} />
                      </button>
                    </div>
                    <h3>{r.title}</h3>
                    <p>{r.reason}</p>
                    <div className="recommendation-sources">
                      {refs.length ? (
                        refs.slice(0, 2).map((e) => (
                          <button key={e.id} onClick={() => onEvidence(e)}>
                            <BookOpen size={12} />
                            {e.source_name || "来源原文"}
                          </button>
                        ))
                      ) : r.match_id ? (
                        <span>依据：已收录比赛赛程</span>
                      ) : (
                        <span>依据：当前球队与跟队设置</span>
                      )}
                    </div>
                    <button
                      className="text-button"
                      onClick={() => onRecommendation(r)}
                    >
                      {
                        {
                          read: "阅读消息",
                          preview: "准备赛前看点",
                          review: "核对赛后表现",
                          follow: "设置持续跟进",
                          verify: "核实这条消息",
                          setup: "完成设置",
                        }[r.kind]
                      }
                      <ArrowRight size={14} />
                    </button>
                  </article>
                );
              })}
            </div>
          ) : (
            <div className="recommendation-empty">
              <Check size={18} />
              <span>
                {mode === "demo"
                  ? "体验示例不产生真实推荐。切换到真实跟队后，推荐会随资料变化更新。"
                  : "暂时没有新的行动建议。收集到比赛与消息进展后，会在这里说明推荐理由。"}
              </span>
            </div>
          )}
          {data.automation_status?.last_error && (
            <p className="automation-inline-error">
              <CircleAlert size={14} />
              上次自动更新未完整完成，可在设置中查看原因。
              <button onClick={onSettings}>查看</button>
            </p>
          )}
          <div className="automation-caption">
            <span
              className={`status-dot ${data.settings.automation?.enabled ? "on" : ""}`}
            />
            {data.settings.automation?.enabled
              ? `自动更新已开启 · ${data.automation_status?.next_check ? `下次 ${dateTime(data.automation_status.next_check)}` : `每 ${data.settings.automation.interval_minutes} 分钟`}`
              : "自动更新未开启"}
            <button className="text-button" onClick={onSettings}>
              调整设置
              <ArrowRight size={12} />
            </button>
          </div>
        </section>
      </div>
    </div>
  );
}

export function TeamCharts({
  matches,
  team,
  mode,
  spoiler,
  onSettings,
}: {
  matches: Match[];
  team?: Team;
  mode: Mode;
  spoiler: boolean;
  onSettings: () => void;
}) {
  const [limit, setLimit] = useState(5);
  const recent = matches
    .filter(
      (m) =>
        m.mode === mode &&
        m.team_id === team?.id &&
        m.status === "finished" &&
        !m.spoiler_hidden &&
        !!m.kickoff &&
        Number.isFinite(Date.parse(m.kickoff)),
    )
    .sort((a, b) => Date.parse(b.kickoff!) - Date.parse(a.kickoff!))
    .slice(0, limit)
    .reverse();
  const rows = recent
    .map((m) => {
      const score = m.score?.match(/^(\d+)\s*[–—:\-]\s*(\d+)$/);
      const gf = m.goals_for ?? (score ? Number(score[1]) : undefined),
        ga = m.goals_against ?? (score ? Number(score[2]) : undefined);
      return { m, gf, ga };
    })
    .filter(
      (r): r is { m: Match; gf: number; ga: number } =>
        r.gf !== undefined &&
        r.ga !== undefined &&
        Number.isFinite(r.gf) &&
        Number.isFinite(r.ga),
    );
  const missing = recent.filter((m) => !rows.some((r) => r.m.id === m.id));
  const won = rows.filter((r) => r.gf > r.ga).length,
    drawn = rows.filter((r) => r.gf === r.ga).length;
  const maxGoals = Math.max(1, ...rows.flatMap((r) => [r.gf, r.ga]));
  const totalGF = rows.reduce((s, r) => s + r.gf, 0),
    totalGA = rows.reduce((s, r) => s + r.ga, 0);
  return (
    <details className="team-charts" open>
      <summary>
        <BarChart3 size={19} />
        <strong>{team?.name} · 近期表现</strong>
        <span>{mode === "demo" ? "虚构示例数据" : "已收录完赛记录"}</span>
        <ChevronDown size={17} />
      </summary>
      {spoiler ? (
        <div className="chart-spoiler">
          <EyeOff size={26} />
          <div>
            <h3>避剧透已开启</h3>
            <p>胜平负和进失球图表已隐藏，避免通过统计泄露赛果。</p>
            <button className="text-button" onClick={onSettings}>
              前往设置关闭避剧透
              <ArrowRight size={14} />
            </button>
          </div>
        </div>
      ) : !rows.length ? (
        <Empty title="还没有可统计的完整比赛记录">
          同步赛程后，仅使用已结束且具有明确比分和日期的比赛。不补齐未知数据。
        </Empty>
      ) : (
        <div className="chart-content">
          <div className="chart-toolbar">
            <p>
              {chartDate(recent[0]?.kickoff)} —{" "}
              {chartDate(recent.at(-1)?.kickoff)} · 已收录 {recent.length} 场
              {recent.length < limit && `（不足 ${limit} 场）`}
              {missing.length > 0 && ` · ${rows.length} 场有完整比分`}
            </p>
            <select
              aria-label="近期表现比赛范围"
              value={limit}
              onChange={(e) => setLimit(Number(e.target.value))}
            >
              <option value={5}>近 5 场</option>
              <option value={10}>近 10 场</option>
            </select>
          </div>
          {missing.length > 0 && (
            <p className="chart-missing">
              <CircleAlert size={13} />
              {missing.length} 场比分缺失，未计入统计：
              {missing.map((m) => m.opponent).join("、")}。未用更早比赛补齐。
            </p>
          )}
          <div className="chart-overview">
            <div>
              <span>胜 / 平 / 负</span>
              <strong>
                {won}
                <i>/</i>
                {drawn}
                <i>/</i>
                {rows.length - won - drawn}
              </strong>
            </div>
            <div>
              <span>进球 / 失球</span>
              <strong>
                {totalGF}
                <i>/</i>
                {totalGA}
              </strong>
            </div>
            <p>
              仅描述这段已收录赛程的结果。
              <br />
              不据此推断战术成因。
            </p>
          </div>
          <div className="chart-legend">
            <strong>逐场进失球</strong>
            <span>
              <i className="goals-for" />
              进球
            </span>
            <span>
              <i className="goals-against" />
              失球
            </span>
            <small>单位：球 · 比分按当前球队在前</small>
          </div>
          <div
            className="goals-chart"
            role="img"
            aria-label={`${team?.name}近期${recent.length}场已收录比赛中，${rows.length}场有完整比分；统计${won}胜${drawn}平${rows.length - won - drawn}负，进${totalGF}球失${totalGA}球。${missing.length ? `另有${missing.length}场比分缺失，未计入。` : ""}`}
          >
            <div className="chart-scale">
              <span>{maxGoals}</span>
              <span>0</span>
            </div>
            <div className="goal-columns">
              {rows.map(({ m, gf, ga }) => (
                <div className="goal-column" key={m.id}>
                  <div className="goal-bars">
                    <div
                      className="goal-bar goals-for"
                      style={{ height: `${(gf / maxGoals) * 100}%` }}
                    >
                      <span>{gf}</span>
                    </div>
                    <div
                      className="goal-bar goals-against"
                      style={{ height: `${(ga / maxGoals) * 100}%` }}
                    >
                      <span>{ga}</span>
                    </div>
                  </div>
                  <span
                    className={`result-chip ${gf > ga ? "win" : gf === ga ? "draw" : "loss"}`}
                  >
                    {gf > ga ? "胜" : gf === ga ? "平" : "负"}
                  </span>
                  <strong title={m.opponent}>{m.opponent}</strong>
                  <time>
                    {new Date(m.kickoff!).toLocaleDateString("zh-CN", {
                      month: "numeric",
                      day: "numeric",
                      timeZone: "Asia/Shanghai",
                    })}
                  </time>
                </div>
              ))}
            </div>
          </div>
          <details className="chart-data">
            <summary>
              逐场数据与来源
              <ChevronDown size={14} />
            </summary>
            <div className="chart-table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>开球时间</th>
                    <th>对手</th>
                    <th>比分</th>
                    <th>来源</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map(({ m, gf, ga }) => (
                    <tr key={m.id}>
                      <td>{dateTime(m.kickoff)}</td>
                      <td>{m.opponent}</td>
                      <td>
                        {gf}–{ga}
                      </td>
                      <td>
                        {safeUrl(m.source_url) ? (
                          <a
                            href={safeUrl(m.source_url)!}
                            target="_blank"
                            rel="noopener noreferrer"
                          >
                            {m.source_name || "原始赛程"}
                            <ArrowUpRight size={12} />
                          </a>
                        ) : mode === "demo" ? (
                          "虚构示例"
                        ) : (
                          "手动录入 · 未提供原始链接"
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </details>
        </div>
      )}
    </details>
  );
}

export function MatchSourceDetails({
  match,
  onEvidence,
  evidence,
}: {
  match: Match;
  onEvidence: (e: Evidence) => void;
  evidence: Evidence[];
}) {
  return (
    <details className="match-source-details">
      <summary>
        <ShieldCheck size={16} />
        赛程来源与名单
        <ChevronDown size={15} />
      </summary>
      <div className="source-status-row">
        <span>
          {match.source_name ||
            (match.mode === "demo" ? "虚构演示赛事" : "手动录入")}
        </span>
        <span>更新：{dateTime(match.synced_at || match.updated_at)}</span>
        {safeUrl(match.source_url) && (
          <a
            href={safeUrl(match.source_url)!}
            target="_blank"
            rel="noopener noreferrer"
          >
            查看原始赛程
            <ArrowUpRight size={13} />
          </a>
        )}
      </div>
      {match.data_warnings?.map((w, i) => (
        <p className="fine-print" key={i}>
          <CircleAlert size={13} />
          {w}
        </p>
      ))}
      {match.spoiler_hidden ? (
        <p className="fine-print">
          <EyeOff size={14} />
          名单与人员资料因避剧透设置隐藏。
        </p>
      ) : (
        <>
          <h3>首发名单</h3>
          {match.lineups?.status === "confirmed" ? (
            <div className="lineup-columns">
              <div>
                <h4>主队</h4>
                <p>{match.lineups.home?.join("、") || "资料未提供"}</p>
              </div>
              <div>
                <h4>客队</h4>
                <p>{match.lineups.away?.join("、") || "资料未提供"}</p>
              </div>
              {safeUrl(match.lineups.source_url) && (
                <a
                  href={safeUrl(match.lineups.source_url)!}
                  target="_blank"
                  rel="noopener noreferrer"
                >
                  名单原文
                  <ArrowUpRight size={12} />
                </a>
              )}
            </div>
          ) : (
            <p className="fine-print">暂未取得可核对的正式首发名单。</p>
          )}
          {!!match.availability?.length && (
            <>
              <h3>人员消息</h3>
              {match.availability.map((item, i) => (
                <div className="availability-row" key={i}>
                  <span>
                    <strong>{item.name}</strong> · {item.status}
                  </span>
                  {item.evidence_id &&
                  evidence.find((e) => e.id === item.evidence_id) ? (
                    <button
                      className="text-button"
                      onClick={() => {
                        const e = evidence.find(
                          (e) => e.id === item.evidence_id,
                        );
                        if (e) onEvidence(e);
                      }}
                    >
                      查看依据
                    </button>
                  ) : safeUrl(item.source_url) ? (
                    <a
                      href={safeUrl(item.source_url)!}
                      target="_blank"
                      rel="noopener noreferrer"
                    >
                      来源原文
                    </a>
                  ) : (
                    <small>未提供来源链接</small>
                  )}
                </div>
              ))}
            </>
          )}
        </>
      )}
    </details>
  );
}

export function AutomationSettings({
  data,
  reload,
  notice,
}: {
  data: AppState;
  reload: () => Promise<void>;
  notice: (m: string, error?: boolean) => void;
}) {
  const [form, setForm] = useState(
    data.settings.automation || {
      enabled: false,
      interval_minutes: 60,
      sync_matches: true,
      generate_brief: false,
    },
  );
  const [verification, setVerification] = useState(
    data.settings.verification_enabled ?? true,
  );
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  async function act(key: string, work: () => Promise<void>) {
    if (busy) return;
    setBusy(key);
    setError("");
    try {
      await work();
      await reload();
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy("");
    }
  }
  return (
    <section id="automation-settings" className="panel form-panel v3-settings">
      <div className="panel-heading">
        <span className="setting-icon">
          <RefreshCw size={20} />
        </span>
        <div>
          <h2>自动跟队</h2>
          <p>{data.runtime?.server_mode ? "服务器按你的节奏更新，关闭浏览器也会继续跟队。" : "服务运行时，按你选择的节奏更新。"}</p>
        </div>
      </div>
      <ErrorNotice>{error}</ErrorNotice>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          act("save", async () => {
            await put("/settings", {
              automation: form,
              verification_enabled: verification,
            });
            notice("自动跟队设置已保存。");
          });
        }}
      >
        <label className="toggle-row">
          <span>
            <strong>自动更新球队资料</strong>
            <small>采集新消息并同步赛程，不变的资料不会重复处理。</small>
          </span>
          <button
            type="button"
            role="switch"
            aria-label="自动更新球队资料"
            aria-checked={form.enabled}
            className={`switch ${form.enabled ? "on" : ""}`}
            onClick={() => setForm({ ...form, enabled: !form.enabled })}
          >
            <span />
          </button>
        </label>
        <label className="spaced">
          更新间隔
          <select
            value={form.interval_minutes}
            onChange={(e) =>
              setForm({ ...form, interval_minutes: Number(e.target.value) })
            }
          >
            {[15, 30, 60, 120, 360, 720, 1440].map((n) => (
              <option value={n} key={n}>
                {n < 60 ? `${n} 分钟` : `${n / 60} 小时`}
              </option>
            ))}
          </select>
        </label>
        <label className="check-row">
          <input
            type="checkbox"
            checked={form.sync_matches}
            onChange={(e) =>
              setForm({ ...form, sync_matches: e.target.checked })
            }
          />
          <span>同步比赛赛程与结果</span>
        </label>
        <label className="check-row">
          <input
            type="checkbox"
            checked={form.generate_brief}
            onChange={(e) =>
              setForm({ ...form, generate_brief: e.target.checked })
            }
          />
          <span>
            另外生成一份球队简报
            <small>汇总本轮球队变化，供助手对话查看。单篇跟队报告由“自动整理跟队报告”单独控制。</small>
          </span>
        </label>
        <label className="check-row">
          <input
            type="checkbox"
            checked={verification}
            onChange={(e) => setVerification(e.target.checked)}
          />
          <span>
            问答与球队简报也核对原文
            <small>
              跟队报告始终核对引用。此开关为普通问答与简报增加原文核对。
            </small>
          </span>
        </label>
        <div className="form-actions">
          <button className="button primary" disabled={!!busy}>
            {busy === "save" ? (
              <Spinner text="保存中" />
            ) : (
              <>
                <Check size={15} />
                保存自动跟队设置
              </>
            )}
          </button>
        </div>
      </form>
      <div className="automation-status">
        <strong>
          {data.automation_status?.running
            ? "正在更新"
            : data.settings.automation?.enabled
              ? "自动跟队已开启"
              : "自动跟队未开启"}
        </strong>
        <p>
          上次检查：{dateTime(data.automation_status?.last_checked)}
          {data.automation_status?.next_check &&
            ` · 下次：${dateTime(data.automation_status.next_check)}`}
        </p>
        {data.automation_status?.last_error && (
          <ErrorNotice>{data.automation_status.last_error}</ErrorNotice>
        )}
        <button
          className="button"
          disabled={!!busy || data.automation_status?.running}
          onClick={() =>
            act("run", async () => {
              await post("/automation/run");
              notice("已开始按已保存的设置更新球队资料。");
            })
          }
        >
          {busy === "run" ? (
            <Spinner text="启动中" />
          ) : (
            <>
              <RefreshCw size={15} />
              按已保存设置立即更新
            </>
          )}
        </button>
      </div>
      {!(data.runtime?.server_mode || data.service?.server_mode) && <details className="service-controls">
        <summary>
          <Monitor size={16} />
          启动与桌面入口
          <ChevronDown size={15} />
        </summary>
        <p className="fine-print">
          本机服务 {data.service?.running ? "运行中" : "状态待确认"} · 看台{" "}
          {data.service?.version || "3.0"}
        </p>
        <button
          className="button"
          disabled={!!busy}
          onClick={() =>
            act("shortcut", async () => {
              await post("/service/shortcut");
              notice("桌面入口已创建，重启电脑后双击即可打开看台。");
            })
          }
        >
          <Download size={15} />
          创建桌面入口
        </button>
        <label className="toggle-row">
          <span>
            <strong>登录电脑后启动服务</strong>
            <small>默认关闭；开启后在后台运行看台。</small>
          </span>
          <button
            type="button"
            role="switch"
            aria-label="登录电脑后启动服务"
            disabled={!!busy}
            aria-checked={!!data.service?.autostart_enabled}
            className={`switch ${data.service?.autostart_enabled ? "on" : ""}`}
            onClick={() =>
              act("autostart", async () => {
                await put("/service/autostart", {
                  enabled: !data.service?.autostart_enabled,
                });
                notice(
                  data.service?.autostart_enabled
                    ? "已关闭登录自启。"
                    : "已开启登录自启。",
                );
              })
            }
          >
            <span />
          </button>
        </label>
      </details>}
    </section>
  );
}
