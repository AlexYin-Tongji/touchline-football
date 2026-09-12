import { useState } from "react";
import { Check, ChevronDown, Search, Star } from "lucide-react";
import { Crest, Empty, Modal } from "./components";
import { findClub } from "./clubCatalog";
import type { League, Team } from "./types";

export function ClubPicker({ teams, teamId, followed, onSelect, leagues = [], seasons = [], season, onSeason }: { teams: Team[]; teamId: string; followed: string[]; onSelect: (id: string) => void; leagues?: League[]; seasons?: { season_start_year: number; label: string }[]; season?: number; onSeason?: (value: number) => void }) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [league, setLeague] = useState("");
  const current = teams.find(team => team.id === teamId);
  const assignedIds = new Set(leagues.flatMap(item => item.teams.map(team => team.id)));
  const selectedLeague = leagues.find(item => item.id === league);
  const eligible = league === "other" ? teams.filter(team => !assignedIds.has(team.id)) : selectedLeague ? selectedLeague.teams : teams;
  const matched = eligible.filter(team => !query || [team.name, team.english_name, team.short_name, ...(findClub(team.id)?.aliases || [])].join(" ").toLowerCase().includes(query.trim().toLowerCase())).sort((a, b) => Number(followed.includes(b.id)) - Number(followed.includes(a.id)));
  return <>
    <div className="club-picker-bar">
      <button className="club-picker-trigger" onClick={() => { setOpen(true); setQuery(""); }} aria-haspopup="dialog" aria-label={`切换球队，当前${current?.name || "未选择"}`}><Crest team={current} small /><strong>{current?.name || "选择球队"}</strong><ChevronDown size={15} /></button>
      <div className="club-shortcuts" aria-label="已关注球队">{teams.filter(team => followed.includes(team.id) && team.id !== teamId).slice(0, 4).map(team => <button key={team.id} onClick={() => onSelect(team.id)}><Crest team={team} small /><span>{team.name}</span></button>)}</div>
      <button className="all-clubs-button" aria-label={`搜索全部 ${teams.length} 支球队`} onClick={() => { setOpen(true); setQuery(""); }}><Search size={15} /><span>全部 {teams.length} 支球队</span></button>
    </div>
    {open && <Modal title="找到你的主场" onClose={() => setOpen(false)} wide>
      <div className="club-directory">
        <label className="search-field"><Search size={18} /><input autoFocus value={query} onChange={e => setQuery(e.target.value)} placeholder="搜索中文名、英文名或球队简称" aria-label="搜索球队" /></label>
        <div className="club-directory-controls"><div className="news-filters"><button className={!league ? "active" : ""} onClick={() => setLeague("")} aria-pressed={!league}>全部球队</button>{leagues.map(value => <button key={value.id} className={league === value.id ? "active" : ""} onClick={() => setLeague(value.id)} aria-pressed={league === value.id}>{value.name}</button>)}{teams.some(team => !assignedIds.has(team.id)) && <button className={league === "other" ? "active" : ""} aria-pressed={league === "other"} onClick={() => setLeague("other")}>其他球队</button>}</div>{seasons.length > 0 && <label>赛季<select aria-label="球队归属赛季" value={season || leagues[0]?.season_start_year} onChange={event => onSeason?.(Number(event.target.value))}>{seasons.map(item => <option key={item.season_start_year} value={item.season_start_year}>{item.label}</option>)}</select></label>}</div>
        <p className="quiet-label">{matched.length} 支球队 · {selectedLeague ? `${selectedLeague.season_label} ${selectedLeague.name}成员 · ` : ""}星标为你已关注的球队。</p>
        <div className="club-directory-grid">{matched.map(team => <button key={team.id} className={team.id === teamId ? "selected" : ""} onClick={() => { onSelect(team.id); setOpen(false); }} aria-pressed={team.id === teamId}><Crest team={team} /><span><strong>{team.name}</strong><small>{team.english_name || team.league_name || team.country}</small></span>{team.id === teamId ? <Check size={17} /> : followed.includes(team.id) ? <Star size={15} fill="currentColor" /> : null}</button>)}</div>
        {!matched.length && <Empty title="没有找到这支球队">试试其他名称，或在设置中添加自定义球队。</Empty>}
      </div>
    </Modal>}
  </>;
}
