import { useCallback, useEffect, useRef, useState } from "react";
import {
  ArrowRight,
  Check,
  ChevronDown,
  CircleAlert,
  Goal,
  MessageCircle,
  Newspaper,
  Plus,
  Search,
  Settings2,
  Shield,
  Star,
  X,
} from "lucide-react";
import { api, errorText, put } from "./api";
import { Crest, Modal, Spinner } from "./components";
import { clubCatalog } from "./clubCatalog";
import { FeedView } from "./v7Feed";
import { TeamCenter } from "./v7TeamCenter";
import { Assistant, type AssistantPrompt } from "./v7Assistant";
import { ModelSettings } from "./v7ModelSettings";
import type { AppState, Team } from "./types";

type Page = "feed" | "team" | "assistant";
const navigation = [
  { id: "feed" as const, label: "动态", icon: Newspaper },
  { id: "team" as const, label: "球队", icon: Shield },
  { id: "assistant" as const, label: "问助手", icon: MessageCircle },
];

export default function App() {
  const [data, setData] = useState<AppState | null>(null);
  const [teamId, setTeamId] = useState(() => {
    try {
      return localStorage.getItem("touchline-team") || "";
    } catch {
      return "";
    }
  });
  const [page, setPage] = useState<Page>("feed");
  const [picker, setPicker] = useState(false);
  const [settings, setSettings] = useState(false);
  const [error, setError] = useState("");
  const [followBusy, setFollowBusy] = useState(false);
  const [prompt, setPrompt] = useState<AssistantPrompt | null>(null);
  const [notice, setNotice] = useState("");
  const scope = useRef(teamId);
  scope.current = teamId;
  const load = useCallback(async () => {
    const requested = scope.current;
    try {
      const next = await api<AppState>(
        `/state?mode=live${requested ? `&team_id=${encodeURIComponent(requested)}` : ""}`,
      );
      if (scope.current !== requested) return;
      setData(next);
      setError("");
      if (!next.teams.some((item) => item.id === requested))
        setTeamId(
          next.settings.followed_teams.find((id) =>
            next.teams.some((item) => item.id === id),
          ) ||
            next.teams[0]?.id ||
            "",
        );
    } catch (err) {
      if (scope.current === requested) setError(errorText(err));
    }
  }, []);
  useEffect(() => {
    void load();
  }, [teamId, load]);
  useEffect(() => {
    if (!teamId) return;
    try {
      localStorage.setItem("touchline-team", teamId);
    } catch {
      /* Storage can be disabled. */
    }
    setPrompt(null);
  }, [teamId]);
  useEffect(() => {
    const timer = window.setInterval(() => {
      if (document.visibilityState === "visible") void load();
    }, 45000);
    return () => clearInterval(timer);
  }, [load]);
  useEffect(() => {
    if (!notice) return;
    const timer = window.setTimeout(() => setNotice(""), 4000);
    return () => clearTimeout(timer);
  }, [notice]);
  const teams = data?.teams || [];
  const team = teams.find((item) => item.id === teamId);
  const followed = data?.settings.followed_teams || [];
  const favoriteTeams = teams.filter((item) => followed.includes(item.id));
  const ask = (value: AssistantPrompt) => {
    setPrompt({ ...value, token: Date.now() });
    setPage("assistant");
  };
  const toggleFollow = async () => {
    if (!data || !team || followBusy) return;
    setFollowBusy(true);
    try {
      const selected = followed.includes(teamId);
      const next = selected
        ? followed.filter((id) => id !== teamId)
        : [...followed, teamId];
      await put("/settings", { followed_teams: next });
      await load();
      setNotice(selected ? `已取消关注${team.name}` : `已关注${team.name}`);
    } catch (err) {
      setNotice(errorText(err));
    } finally {
      setFollowBusy(false);
    }
  };
  return (
    <div className="v7-app">
      <header className="site-header">
        <a
          href="#"
          className="brand"
          aria-label="看台首页"
          onClick={(e) => {
            e.preventDefault();
            setPage("feed");
          }}
        >
          <span className="brand-mark">
            <Goal size={25} strokeWidth={1.6} />
          </span>
          <span>
            看台<small>为热爱留一席</small>
          </span>
        </a>
        <nav className="main-tabs" aria-label="主导航">
          {navigation.map((item) => (
            <button
              key={item.id}
              className={page === item.id ? "active" : ""}
              aria-current={page === item.id ? "page" : undefined}
              onClick={() => setPage(item.id)}
            >
              <item.icon size={18} />
              <span>{item.label}</span>
            </button>
          ))}
        </nav>
        <div className="header-end">
          <span className="personal-label">我的足球主场</span>
          <button
            className="icon-button"
            aria-label="模型配置"
            title="模型配置"
            onClick={() => setSettings(true)}
          >
            <Settings2 size={20} />
          </button>
        </div>
      </header>
      <div className="workspace">
        <aside className="team-rail">
          <div className="rail-title">
            <span>我的球队</span>
            <button
              className="icon-button"
              onClick={() => setPicker(true)}
              aria-label="寻找球队"
            >
              <Plus size={17} />
            </button>
          </div>
          <div className="favorite-teams">
            {favoriteTeams.map((item) => (
              <button
                key={item.id}
                className={item.id === teamId ? "active" : ""}
                onClick={() => setTeamId(item.id)}
              >
                <Crest team={item} small />
                <span>{item.name}</span>
                {item.id === teamId && <span className="selected-dot" />}
              </button>
            ))}
          </div>
          {!favoriteTeams.length && data && (
            <p className="rail-hint">
              关注一支球队，
              <br />
              从这里开启你的主场。
            </p>
          )}
          <button className="explore-teams" onClick={() => setPicker(true)}>
            <Search size={16} />
            发现更多球队
            <ArrowRight size={14} />
          </button>
          <div className="rail-bottom">
            <span className="mini-pitch" aria-hidden="true">
              <i />
            </span>
            <p>
              一支球队，
              <br />
              不止九十分钟。
            </p>
            <small>英超 · 西甲 · 德甲 · 意甲 · 法甲</small>
          </div>
        </aside>
        <main className={`main-content page-${page}`}>
          {data && team && (
            <div className="team-context">
              <button
                className="current-team"
                onClick={() => setPicker(true)}
                aria-label={`切换球队，当前${team.name}`}
              >
                <Crest team={team} small />
                <strong>{team.name}</strong>
                <ChevronDown size={14} />
              </button>
              <span className="team-context-league">
                {team.league_name || team.league || team.country}
              </span>
              <button
                className={`follow-button ${followed.includes(teamId) ? "following" : ""}`}
                disabled={followBusy}
                onClick={toggleFollow}
              >
                <Star
                  size={14}
                  fill={followed.includes(teamId) ? "currentColor" : "none"}
                />
                {followed.includes(teamId) ? "已关注" : "关注球队"}
              </button>
            </div>
          )}
          {error && (
            <div className="notice error">
              <CircleAlert size={17} />
              <span>{error}</span>
              <button onClick={load}>重新连接</button>
            </div>
          )}
          {!data ? (
            <div className="page-loading">
              <Spinner text="正在打开你的主场" />
            </div>
          ) : team ? (
            <>
              {page === "feed" && (
                <FeedView
                  key={team.id}
                  team={team}
                  data={data}
                  ask={ask}
                  openModel={() => setSettings(true)}
                />
              )}
              {page === "team" && (
                <TeamCenter key={team.id} team={team} data={data} ask={ask} />
              )}
              <div hidden={page !== "assistant"}>
                <Assistant
                  key={team.id}
                  team={team}
                  data={data}
                  incoming={prompt}
                  openModel={() => setSettings(true)}
                  onChanged={load}
                />
              </div>
            </>
          ) : (
            <div className="empty-state">
              <Shield />
              <h2>找到你的球队</h2>
              <p>选择一支球队，开始阅读动态、了解球员和比赛。</p>
              <button
                className="button primary"
                onClick={() => setPicker(true)}
              >
                选择球队
              </button>
            </div>
          )}
        </main>
      </div>
      {picker && (
        <TeamPicker
          teams={teams}
          followed={followed}
          current={teamId}
          onSelect={(id) => {
            setTeamId(id);
            setPicker(false);
          }}
          onClose={() => setPicker(false)}
        />
      )}
      {settings && (
        <Modal title="模型配置" onClose={() => setSettings(false)} wide>
          <ModelSettings onChanged={load} />
        </Modal>
      )}
      {notice && (
        <div className="toast" role="status">
          <Check size={16} />
          {notice}
          <button
            className="icon-button"
            aria-label="关闭通知"
            onClick={() => setNotice("")}
          >
            <X size={15} />
          </button>
        </div>
      )}
    </div>
  );
}

function TeamPicker({
  teams,
  followed,
  current,
  onSelect,
  onClose,
}: {
  teams: Team[];
  followed: string[];
  current: string;
  onSelect: (id: string) => void;
  onClose: () => void;
}) {
  const [query, setQuery] = useState("");
  const [country, setCountry] = useState("");
  const [leagueTeams, setLeagueTeams] = useState<Record<string, string[]>>({});
  const [leagueError, setLeagueError] = useState(false);
  const countries = [
    { id: "PL", name: "英超" },
    { id: "PD", name: "西甲" },
    { id: "BL1", name: "德甲" },
    { id: "SA", name: "意甲" },
    { id: "FL1", name: "法甲" },
  ];
  useEffect(() => {
    let valid = true;
    api<{ leagues: { id: string; teams: Team[] }[] }>("/leagues")
      .then((value) => {
        if (valid)
          setLeagueTeams(
            Object.fromEntries(
              value.leagues.map((league) => [
                league.id,
                league.teams.map((team) => team.id),
              ]),
            ),
          );
      })
      .catch(() => {
        if (valid) setLeagueError(true);
      });
    return () => {
      valid = false;
    };
  }, []);
  const countryMatches = (team: Team) =>
    !country || leagueTeams[country]?.includes(team.id);
  const results = teams
    .filter(
      (item) =>
        countryMatches(item) &&
        [
          item.name,
          item.english_name,
          item.short_name,
          item.city,
          ...(clubCatalog.find((club) => club.id === item.id)?.aliases || []),
        ]
          .join(" ")
          .toLowerCase()
          .includes(query.trim().toLowerCase()),
    )
    .sort(
      (a, b) =>
        Number(followed.includes(b.id)) - Number(followed.includes(a.id)),
    );
  return (
    <Modal title="找到你的主场" onClose={onClose} wide>
      <div className="team-picker">
        <label className="search-input">
          <Search size={18} />
          <input
            autoFocus
            aria-label="搜索球队"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="搜索球队、城市或英文名"
          />
        </label>
        <div className="filter-tabs">
          <button
            className={!country ? "active" : ""}
            onClick={() => setCountry("")}
          >
            全部球队
          </button>
          {countries.map((item) => (
            <button
              key={item.id}
              className={country === item.id ? "active" : ""}
              disabled={!leagueTeams[item.id]}
              onClick={() => setCountry(item.id)}
            >
              {item.name}
            </button>
          ))}
        </div>
        {leagueError && (
          <p className="muted">联赛筛选暂时不可用，你仍然可以搜索球队。</p>
        )}
        <div className="picker-count">{results.length} 支球队</div>
        <div className="picker-grid">
          {results.map((item) => (
            <button
              key={item.id}
              className={current === item.id ? "selected" : ""}
              onClick={() => onSelect(item.id)}
            >
              <Crest team={item} />
              <span>
                <strong>{item.name}</strong>
                <small>{item.english_name}</small>
              </span>
              {current === item.id ? (
                <Check size={17} />
              ) : followed.includes(item.id) ? (
                <Star size={15} />
              ) : null}
            </button>
          ))}
        </div>
        {!results.length && (
          <div className="empty-state">
            <Search />
            <h3>没有找到这支球队</h3>
            <p>试试中文名、英文名或常用简称。</p>
          </div>
        )}
      </div>
    </Modal>
  );
}
