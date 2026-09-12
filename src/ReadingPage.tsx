import { useMemo, useState } from "react";
import { ArrowRight, BookOpen, Clock3, FilePlus2, Search } from "lucide-react";
import { contentLabel, dateTime, Empty, evidenceTitle, sourceLabel } from "./components";
import type { AutoReport, Evidence, Team } from "./types";

export function ReadingPage({ evidence, team, onRead, onSources, reports = [], onReport }: { evidence: Evidence[]; team: Team; onRead: (item: Evidence) => void; onSources: () => void; reports?: AutoReport[]; onReport?: (report: AutoReport) => void }) {
  const [query, setQuery] = useState("");
  const [source, setSource] = useState("");
  const [fullOnly, setFullOnly] = useState(false);
  const [shown, setShown] = useState(16);
  const articles = evidence.filter(item => !item.fixture_record && !item.spoiler_hidden);
  const sources = [...new Set(articles.map(sourceLabel))];
  const fullCount = articles.filter(item => ["full_text", "imported"].includes(item.content_status || "") && !item.text_truncated).length;
  const visible = useMemo(() => articles.filter(item => (!source || sourceLabel(item) === source) && (!fullOnly || ["full_text", "imported"].includes(item.content_status || "") && !item.text_truncated) && (!query || `${item.title} ${item.text} ${sourceLabel(item)}`.toLowerCase().includes(query.trim().toLowerCase()))).sort((a,b) => (b.published_at || b.fetched_at || "").localeCompare(a.published_at || a.fetched_at || "")), [articles, source, fullOnly, query]);
  return <div className="reading-page">
    <div className="hub-title-row"><div><span className="overline">{team.name} · 阅读室</span><h1>消息之外，读懂来龙去脉。</h1><p className="muted">原始报道、发布会和深度资料，都在这里。</p></div><button className="button" onClick={onSources}><FilePlus2 size={17} />收集资料</button></div>
    <div className="reading-summary"><span><BookOpen size={18} /><strong>{articles.length}</strong>份报道</span><span><strong>{fullCount}</strong>份已提取正文或导入资料</span><span><strong>{sources.length}</strong>个来源</span></div>
    <div className="reading-filters"><label className="search-field"><Search size={18} /><input aria-label="搜索报道" placeholder="搜索标题、正文或来源" value={query} onChange={e => { setQuery(e.target.value); setShown(16); }} /></label><select aria-label="筛选报道来源" value={source} onChange={e => { setSource(e.target.value); setShown(16); }}><option value="">所有来源</option>{sources.map(value => <option key={value}>{value}</option>)}</select><label className="reading-check"><input type="checkbox" checked={fullOnly} onChange={e => { setFullOnly(e.target.checked); setShown(16); }} />已提取正文 / 导入</label></div>
    <div className="reading-results-heading"><span>{visible.length} 份资料</span><span>按发布时间排列 · 北京时间</span></div>
    <div className="reading-list">{visible.slice(0, shown).map(item => <article className="reading-card" key={item.id}><div className="reading-card-meta"><span>{sourceLabel(item)}</span><span className={`content-badge ${item.content_status || "unknown"}`}>{contentLabel(item)}</span>{item.mode === "demo" && <span className="tag">虚构示例</span>}</div><button className="reading-title" onClick={() => onRead(item)}><h2>{evidenceTitle(item)}</h2><ArrowRight size={20} /></button><p className="reading-excerpt">{item.text?.replace(/\s+/g, " ").slice(0, 240) || "尚未取得正文，可打开来源阅读。"}</p>{reports.filter(report => report.status === "completed" && report.report?.sources.some(source => source.id === item.id)).slice(0, 1).map(report => <button key={report.id} className="reading-report-link" onClick={() => onReport?.(report)}>中文跟队报告已整理<ArrowRight size={14} /></button>)}<div className="reading-card-footer"><span>{item.published_at ? dateTime(item.published_at) : "发布时间未知"}</span><span><Clock3 size={13} />约 {item.reading_minutes || Math.max(1, Math.ceil((item.text?.length || 0) / 900))} 分钟</span><button onClick={() => onRead(item)}>阅读资料<ArrowRight size={14} /></button></div></article>)}</div>
    {!visible.length && <Empty title={articles.length ? "没有符合条件的报道" : "给你的阅读室添上第一篇报道"} action={<button className="button" onClick={articles.length ? () => { setQuery(""); setSource(""); setFullOnly(false); } : onSources}>{articles.length ? "清除筛选" : "查找和导入资料"}</button>}>{articles.length ? "尝试其他关键词，或放宽筛选条件。" : "搜索球队消息，或导入一篇你想和助手一起读的报道。"}</Empty>}
    {visible.length > shown && <button className="more-news" onClick={() => setShown(shown + 16)}>继续阅读更多报道<ArrowRight size={16} /></button>}
  </div>;
}
