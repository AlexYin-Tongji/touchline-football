import { useState } from "react";
import { Check, ChevronDown, Search, Star } from "lucide-react";
import { Crest, Empty, Modal } from "./components";
import { findClub } from "./clubCatalog";
import type { Team } from "./types";

export function ClubPicker({ teams, teamId, followed, onSelect }: { teams: Team[]; teamId: string; followed: string[]; onSelect: (id: string) => void }) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [league, setLeague] = useState("");
  const current = teams.find(team => team.id === teamId);
  const region = (team: Team) => team.country || team.league_name || team.league || "其他";
  const leagues = [...new Set(teams.map(region))];
  const matched = teams.filter(team => (!league || region(team) === league) && (!query || [team.name, team.english_name, team.short_name, ...(findClub(team.id)?.aliases || [])].join(" ").toLowerCase().includes(query.trim().toLowerCase()))).sort((a, b) => Number(followed.includes(b.id)) - Number(followed.includes(a.id)));
  return <>
    <div className="club-picker-bar">
      <button className="club-picker-trigger" onClick={() => { setOpen(true); setQuery(""); }} aria-haspopup="dialog" aria-label={`切换球队，当前${current?.name || "未选择"}`}><Crest team={current} small /><strong>{current?.name || "选择球队"}</strong><ChevronDown size={15} /></button>
      <div className="club-shortcuts" aria-label="已关注球队">{teams.filter(team => followed.includes(team.id) && team.id !== teamId).slice(0, 4).map(team => <button key={team.id} onClick={() => onSelect(team.id)}><Crest team={team} small /><span>{team.name}</span></button>)}</div>
      <button className="all-clubs-button" aria-label={`搜索全部 ${teams.length} 支球队`} onClick={() => setOpen(true)}><Search size={15} /><span>全部 {teams.length} 支球队</span></button>
    </div>
    {open && <Modal title="找到你的主场" onClose={() => setOpen(false)} wide>
      <div className="club-directory">
        <label className="search-field"><Search size={18} /><input autoFocus value={query} onChange={e => setQuery(e.target.value)} placeholder="搜索中文名、英文名或球队简称" aria-label="搜索球队" /></label>
        <div className="news-filters"><button className={!league ? "active" : ""} onClick={() => setLeague("")} aria-pressed={!league}>全部球队</button>{leagues.map(value => <button key={value} className={league === value ? "active" : ""} onClick={() => setLeague(value)} aria-pressed={league === value}>{value}</button>)}</div>
        <p className="quiet-label">{matched.length} 支球队 · 星标为你已关注的球队，可在设置中调整。</p>
        <div className="club-directory-grid">{matched.map(team => <button key={team.id} className={team.id === teamId ? "selected" : ""} onClick={() => { onSelect(team.id); setOpen(false); }} aria-pressed={team.id === teamId}><Crest team={team} /><span><strong>{team.name}</strong><small>{team.english_name || team.league_name || team.country}</small></span>{team.id === teamId ? <Check size={17} /> : followed.includes(team.id) ? <Star size={15} fill="currentColor" /> : null}</button>)}</div>
        {!matched.length && <Empty title="没有找到这支球队">试试其他名称，或在设置中添加自定义球队。</Empty>}
      </div>
    </Modal>}
  </>;
}
