import { useEffect, useState } from "react";
import { ArrowLeft, ArrowRight, ArrowUpRight, BookOpen, CheckCircle2, Clock3, FileText, RefreshCw, Settings2, Sparkles } from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { api, errorText } from "./api";
import { dateTime, Empty, ErrorNotice, safeUrl, Spinner } from "./components";
import type { AutoReport, Evidence, Team } from "./types";

const pendingLabels: Record<AutoReport["status"], string> = { queued: "等待整理", generating: "正在写作与核对", completed: "报告已完成", needs_configuration: "等待连接助手", failed: "本次整理未完成", stale: "有新资料，等待更新" };
export const isReadyReport = (item: AutoReport) => item.status === "completed" && !!item.report;

export function ReportShelf({ reports, onRead, onSettings, onRetry, compact = false, configured = false, automatic = true, onAll, onSources }: { reports: AutoReport[]; onRead: (report: AutoReport) => void; onSettings: () => void; onRetry: (report: AutoReport) => void; compact?: boolean; configured?: boolean; automatic?: boolean; onAll?: () => void; onSources?: () => void }) {
  const [queueShown, setQueueShown] = useState(30);
  const ready = reports.filter(isReadyReport).sort((a, b) => (b.completed_at || b.updated_at || "").localeCompare(a.completed_at || a.updated_at || ""));
  const pending = reports.filter(item => !isReadyReport(item));
  return <section className={`report-shelf ${compact ? "compact" : ""}`} aria-label="助手跟队报告">
    <div className="hub-section-heading"><h2><Sparkles size={19} />跟队报告</h2>{onAll ? <button className="text-button" onClick={onAll}>全部报告<ArrowRight size={15} /></button> : <span className="quiet-label">{ready.length} 篇已完成{pending.length ? ` · ${pending.length} 篇待整理` : ""}</span>}</div>
    <p className="report-section-caption">把原始报道串成故事：发生了什么、为什么值得关注、接下来看什么。</p>
    {!configured && <div className="report-setup"><div><Settings2 size={22} /><span><strong>连接助手，让报道自动成为中文报告</strong><small>{automatic ? "保存可用的模型后，Agent 会整理已收录资料，并在后续更新时继续写作。" : "当前自动整理已暂停。连接助手后，可在跟队偏好中开启自动写报告。"}</small></span></div><button className="button dark-button" onClick={onSettings}>连接助手<ArrowRight size={15} /></button></div>}
    {configured && !automatic && <div className="report-setup"><div><Settings2 size={20} /><span><strong>自动整理已暂停</strong><small>已完成的报告仍可阅读；恢复后会继续处理新的资料。</small></span></div><button className="button" onClick={onSettings}>调整自动整理</button></div>}
    <div className="report-card-grid">{ready.slice(0, compact ? 2 : undefined).map((item, index) => <article className={`report-card ${index === 0 ? "featured" : ""}`} key={item.id}>
      <div className="report-card-kicker"><span><Sparkles size={13} />Agent 报告</span><time>{dateTime(item.completed_at || item.updated_at)}</time></div>
      <button className="report-title-button" onClick={() => onRead(item)}><h3>{item.report!.title}</h3><ArrowUpRight size={20} /></button>
      <p className="report-lead">{item.report!.lead.replace(/\[([^\]\n]+)\]/g, (value, id: string) => { const index = item.report!.sources.findIndex(source => source.id === id); return index >= 0 ? ` [${index + 1}]` : value; })}</p>
      <div className="report-card-footer"><span>{item.report!.sources.length} 份引用资料 · 约 {item.report!.reading_minutes || 3} 分钟</span><button onClick={() => onRead(item)}>读完整报告<ArrowRight size={15} /></button></div>
    </article>)}</div>
    {!ready.length && configured && <div className="report-empty"><FileText size={30} /><div><h3>{pending.length ? "报告正在路上" : "你的第一篇跟队报告，从一份资料开始"}</h3><p>{pending.length ? "下方会显示实际整理进度。完成后，完整中文报告会自动出现在这里。" : "更新球队动态或导入报道，Agent 会结合你的关注点整理成文。"}</p></div>{!pending.length && onSources && <button className="button" onClick={onSources}>收集资料</button>}</div>}
    {pending.length > 0 && <details className="report-queue" open={!ready.length}><summary><span><Clock3 size={16} />资料整理进度</span><span>{pending.length} 篇</span></summary><div>{pending.slice(0, compact ? 4 : queueShown).map(item => <article className="report-queue-item" key={item.id}><span className={`report-status ${item.status}`}>{["generating", "queued"].includes(item.status) ? <RefreshCw className={item.status === "generating" ? "spin" : ""} size={13} /> : <FileText size={13} />}{pendingLabels[item.status]}</span><button className="report-queue-title" onClick={() => onRead(item)}>{item.source_title || "已收录报道"}</button>{item.source_excerpt && <p>{item.source_excerpt.slice(0, 190)}{item.source_excerpt.length > 190 ? "…" : ""}</p>}{item.source_count === 0 ? <p className="report-budget-note">当前阅读范围没有可用原文</p> : item.queue_note && <p className="report-budget-note">{item.queue_note}</p>}<div className="report-queue-actions"><span>{item.source_count ?? 1} 份来源 · {dateTime(item.updated_at || item.created_at)}</span>{item.status === "needs_configuration" ? <button onClick={onSettings}>连接助手<ArrowRight size={13} /></button> : ["failed", "stale"].includes(item.status) ? <button disabled={item.source_count === 0} onClick={() => onRetry(item)}><RefreshCw size={13} />重新整理</button> : <button onClick={() => onRead(item)}>查看进度<ArrowRight size={13} /></button>}</div></article>)}</div>{!compact && pending.length > queueShown && <button className="more-news" onClick={() => setQueueShown(value => value + 30)}>展开更多整理进度<ArrowRight size={15} /></button>}{compact && pending.length > 4 && onAll && <button className="more-news" onClick={onAll}>查看全部 {pending.length} 篇待整理资料<ArrowRight size={15} /></button>}</details>}
  </section>;
}

export function ReportsPage({ team, reports, ...actions }: { team: Team; reports: AutoReport[]; onRead: (report: AutoReport) => void; onSettings: () => void; onRetry: (report: AutoReport) => void; onSources: () => void; configured: boolean; automatic: boolean }) {
  const [query, setQuery] = useState("");
  const [tab, setTab] = useState("all");
  const visible = reports.filter(item => (tab === "all" || (tab === "ready" ? isReadyReport(item) : !isReadyReport(item))) && `${item.report?.title || item.source_title} ${item.report?.lead || item.source_excerpt || ""}`.toLowerCase().includes(query.toLowerCase()));
  return <div className="reports-page"><div className="hub-title-row"><div><span className="overline">{team.name} · 跟队阅读</span><h1>{reports.some(isReadyReport) ? "报道里的故事，已经替你理好。" : "让每篇报道，都有来龙去脉。"}</h1><p className="muted">根据你的兴趣自动整理，保留原文出处，也留出继续追问的空间。</p></div><button className="button" onClick={actions.onSources}><BookOpen size={17} />收集资料</button></div><div className="report-page-filters"><div className="news-filters" aria-label="报告状态">{[["all", "全部"], ["ready", "可以阅读"], ["pending", "正在整理"]].map(([id, label]) => <button key={id} aria-pressed={tab === id} className={tab === id ? "active" : ""} onClick={() => setTab(id)}>{label}</button>)}</div><input type="search" aria-label="搜索跟队报告" value={query} onChange={event => setQuery(event.target.value)} placeholder="搜索报告与来源标题" /></div>{(visible.length > 0 || !reports.length) && <ReportShelf reports={visible} {...actions} />}{!!reports.length && !visible.length && <Empty title="没有符合条件的报告" action={<button className="button" onClick={() => { setQuery(""); setTab("all"); }}>清除筛选</button>}>试试其他关键词，或查看全部整理进度。</Empty>}</div>;
}

function ReportMarkdown({ text, sources, onCite }: { text: string; sources: NonNullable<AutoReport["report"]>["sources"]; onCite: (id: string) => void }) {
  const ids = sources.map(source => source.id);
  const content = text.replace(/\[([^\]\n]+)\]/g, (value, id: string) => ids.includes(id) ? `[${ids.indexOf(id) + 1}](#report-source-${encodeURIComponent(id)})` : value);
  return <ReactMarkdown remarkPlugins={[remarkGfm]} components={{ img: ({ alt }) => <span>{alt || ""}</span>, a: ({ href, children }) => {
    const id = ids.find(id => href === `#report-source-${encodeURIComponent(id)}`);
    if (id) return <button className="citation" onClick={() => onCite(id)} title={sources.find(source => source.id === id)?.title}>{children}</button>;
    const url = safeUrl(href);
    return url ? <a href={url} target="_blank" rel="noopener noreferrer">{children}</a> : <span>{children}</span>;
  } }}>{content}</ReactMarkdown>;
}

export function ReportReader({ item, team, evidence, spoiler, onBack, onEvidence, onAsk, onSettings, onRetry }: { item: AutoReport; team: Team; evidence: Evidence[]; spoiler: boolean; onBack: () => void; onEvidence: (item: Evidence) => void; onAsk: (question: string, report: AutoReport) => void; onSettings: () => void; onRetry: (report: AutoReport) => void }) {
  const [detail, setDetail] = useState(item);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [large, setLarge] = useState(false);
  useEffect(() => { setDetail(item); }, [item]);
  useEffect(() => {
    let current = true;
    let timer: number;
    const read = async () => {
      try {
        const result = await api<AutoReport>(`/reports/${encodeURIComponent(item.id)}`);
        if (!current) return;
        setDetail(result); setError("");
        if (["queued", "generating"].includes(result.status)) timer = window.setTimeout(read, result.status === "generating" ? 2500 : 10000);
      } catch (failure) { if (current) setError(errorText(failure)); }
      finally { if (current) setLoading(false); }
    };
    setLoading(true); void read();
    return () => { current = false; window.clearTimeout(timer); };
  }, [item.id, item.status, item.updated_at, spoiler]);
  const report = isReadyReport(detail) ? detail.report : undefined;
  const cite = (id: string) => {
    const found = evidence.find(source => source.id === id);
    if (found) onEvidence(found);
    else { const source = report?.sources.find(source => source.id === id); if (source) onEvidence({ ...source, id, mode: detail.mode, team_id: detail.team_id, text: "", origin: "report" }); }
  };
  return <div className={`report-reader ${large ? "large-type" : ""}`}>
    <div className="report-reader-toolbar"><button className="text-button" onClick={onBack}><ArrowLeft size={17} />返回报告</button><button className="button" aria-pressed={large} onClick={() => setLarge(!large)}>{large ? "标准字号" : "大字阅读"}</button></div>
    <ErrorNotice>{error}</ErrorNotice>
    {loading && <Spinner text="正在打开最新报告" />}
    <article className="report-paper"><div className="report-paper-kicker"><span><Sparkles size={15} />{team.name} · {report ? "跟队报告" : "资料整理"}</span><span>{dateTime(detail.completed_at || detail.updated_at)}</span></div>
      <h1>{report?.title || detail.source_title || "资料整理进度"}</h1>
      {report ? <>
        <div className="report-standfirst"><ReportMarkdown text={report.lead} sources={report.sources} onCite={cite} /></div>
        <div className="report-paper-meta"><span><Clock3 size={14} />约 {report.reading_minutes || 3} 分钟</span><span><BookOpen size={14} />{report.sources.length} 份引用资料</span>{detail.verification_status === "supported" && <span><CheckCircle2 size={14} />已核对引用</span>}</div>
        <div className="report-narrative">{report.sections.map((section, index) => <section key={`${section.heading}-${index}`}><h2>{section.heading}</h2><ReportMarkdown text={section.body} sources={report.sources} onCite={cite} />{section.evidence_ids?.length > 0 && <div className="report-inline-sources" aria-label={`${section.heading}的引用资料`}>{section.evidence_ids.map(id => { const index = report.sources.findIndex(source => source.id === id); return <button key={id} onClick={() => cite(id)}><BookOpen size={12} />{index >= 0 ? `来源 ${index + 1}` : "阅读来源"}</button>; })}</div>}</section>)}
          {report.analysis && <section className="report-analysis"><span className="overline">助手分析</span><h2>这对球队意味着什么</h2><ReportMarkdown text={report.analysis} sources={report.sources} onCite={cite} /></section>}
          {!!report.next_steps?.length && <section className="report-next"><h2>接下来，留意这些变化</h2><ul>{report.next_steps.map((step, index) => <li key={index}><ReportMarkdown text={step} sources={report.sources} onCite={cite} /></li>)}</ul></section>}
        </div>
        <section className="report-bibliography"><h2>本篇引用的资料</h2>{report.sources.map((source, index) => <div key={source.id}><span className="report-source-number">{index + 1}</span><button onClick={() => cite(source.id)}><strong>{source.title}</strong><small>{source.source_name || "原始来源"}{source.published_at ? ` · ${dateTime(source.published_at)}` : ""}</small></button>{safeUrl(source.url) && <a href={safeUrl(source.url)!} target="_blank" rel="noopener noreferrer" aria-label={`在新窗口打开：${source.title}`} title="打开来源网站"><ArrowUpRight size={17} /></a>}</div>)}</section>
        <div className="report-followup"><div><h3>有些细节，值得再聊聊。</h3><p>带着本篇的原始资料，继续追问助手。</p></div><button className="button dark-button" onClick={() => onAsk(`结合报告《${report.title}》的引用资料，进一步解释它对${team.name}的影响。`, detail)}>围绕本篇追问<ArrowRight size={16} /></button></div>
      </> : <div className="report-pending-detail"><span className={`report-status ${detail.status}`}><RefreshCw size={15} className={detail.status === "generating" ? "spin" : ""} />{pendingLabels[detail.status]}</span><p>{detail.status === "needs_configuration" ? "资料已经保存。连接模型后，Agent 会阅读来源、整理中文报告并核对引用。" : detail.status === "failed" ? "这次没有生成可供阅读的完整报告，可以重新整理；原始资料仍保留在阅读室。" : detail.status === "stale" ? detail.source_count === 0 ? "当前阅读范围没有可用原文。调整避剧透偏好或补充资料后，Agent 才能继续整理。" : "引用资料或阅读偏好有变化，Agent 将按最新内容重新整理。" : "Agent 会依次阅读资料、整理事件背景与影响，再核对引用。完成后这里会自动显示报告。"}</p>{detail.queue_note && <p className="report-budget-note">{detail.queue_note}</p>}{detail.error && <details><summary>查看具体原因</summary><p>{detail.error}</p></details>}{detail.source_excerpt && <section><h2>来源摘录</h2><p className="source-excerpt" lang={detail.source_language === "zh" ? "zh-CN" : undefined}>{detail.source_excerpt}</p></section>}{!!detail.evidence_ids?.length && <section className="report-bibliography"><h2>查看已收录原文</h2>{detail.evidence_ids.map(id => { const source = evidence.find(source => source.id === id); return source && <div key={id}><button onClick={() => onEvidence(source)}><strong>{source.title}</strong><small>{source.source_name || "原始来源"}</small></button><BookOpen size={16} /></div>; })}</section>}{detail.status === "needs_configuration" ? <button className="button dark-button" onClick={onSettings}>连接助手<ArrowRight size={16} /></button> : ["failed", "stale"].includes(detail.status) && <button className="button dark-button" disabled={detail.source_count === 0} onClick={() => onRetry(detail)}><RefreshCw size={16} />重新整理报告</button>}</div>}
    </article>
  </div>;
}
