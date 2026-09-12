import { useEffect, useRef, useState, type ReactNode } from "react";
import {
  ArrowUpRight,
  BookOpen,
  ChevronDown,
  CircleAlert,
  CircleCheck,
  LoaderCircle,
  Square,
  X,
} from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { Evidence, Run, Team } from "./types";

export function dateTime(value?: string | null) {
  if (!value) return "时间未知";
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? "时间未知"
    : new Intl.DateTimeFormat("zh-CN", {
        timeZone: "Asia/Shanghai",
        month: "numeric",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
        hour12: false,
      }).format(date);
}
export function safeUrl(url?: string) {
  try {
    const parsed = new URL(url || "");
    return ["http:", "https:"].includes(parsed.protocol) ? parsed.href : null;
  } catch {
    return null;
  }
}
export const skillNames: Record<string, string> = {
  auto: "智能判断",
  event_watch: "事件跟进",
  ask: "证据问答",
  team_brief: "球队变化",
  match_preview: "赛前观察",
  match_review: "赛后复盘",
};
export const statusNames: Record<string, string> = {
  queued: "准备中",
  running: "正在查阅资料",
  completed: "回答完成",
  failed: "未完成",
  cancelled: "已停止",
};
export function sourceLabel(item: Pick<Evidence, "source_name" | "origin" | "url">) {
  const name = item.source_name?.trim() || "";
  const url = safeUrl(item.url);
  const host = url ? new URL(url).hostname.toLowerCase() : "";
  const officialSites: Record<string, string> = {
    "arsenal.com": "阿森纳官网",
    "liverpoolfc.com": "利物浦官网",
    "fcbarcelona.com": "巴塞罗那官网",
    "inter.it": "国际米兰官网",
  };
  for (const [domain, label] of Object.entries(officialSites)) {
    if (host === domain || host.endsWith(`.${domain}`)) return label;
  }
  if (/bbc/i.test(name)) return "英国广播公司体育";
  if (/sky/i.test(name)) return "天空体育";
  if (/guardian/i.test(name)) return "卫报体育";
  if (/arsenal/i.test(name)) return "阿森纳相关资讯";
  if (/liverpool/i.test(name)) return "利物浦相关资讯";
  if (/barcelona/i.test(name)) return "巴塞罗那相关资讯";
  if (/inter(?:\b|nazionale)/i.test(name)) return "国际米兰相关资讯";
  if (/espn/i.test(name)) return "ESPN 体育";
  if (/[\u3400-\u9fff]/.test(name) && !/[A-Za-z]{4,}/.test(name)) return name;
  return item.origin === "manual" ? "本地资料" : "资讯来源";
}
export function evidenceTitle(item: Pick<Evidence, "title" | "source_name" | "origin" | "url">) {
  return /[\u3400-\u9fff]/.test(item.title || "")
    ? item.title
    : `${sourceLabel(item)} · 相关报道`;
}
function runErrorMessage(error?: string | null) {
  if (!error) return null;
  if (/密钥|api.?key|401|403|授权|权限/i.test(error))
    return "助手暂时无法连接，请在设置中检查服务地址和密钥。";
  if (/超时|timeout|网络|连接|代理|http|请求失败/i.test(error))
    return "暂时无法连接服务，请稍后重试。";
  if (/最大.*轮次|轮次.*上限/.test(error))
    return "这次查询尚未完成，可以继续查询，或把问题问得更具体。";
  if (/引用|核对|证据|摘录|原文/.test(error))
    return "暂时没有足够可靠的资料完成回答，可以补充来源后重试。";
  if (/未.*配置|未.*设置|请先.*配置|模型不存在/.test(error))
    return "请先在设置中连接助手服务。";
  return "这次回答未能完成，请重试。";
}
function answerNotices(run: Run) {
  if (run.mode === "demo") return [];
  const notices: string[] = [];
  if (run.answer && run.status === "completed") {
    const verification = run.verification?.status;
    if (verification === "partial") notices.push("部分信息仍待确认，请结合来源判断。");
    else if (verification === "insufficient") notices.push("现有资料不足以确认全部信息。");
    else if (verification !== "supported") notices.push("本次回答尚未完成来源核对。");
  }
  if ((run.warnings || []).some((warning) => /来源.*未能读取|来源.*无法读取/.test(warning)))
    notices.push("部分来源暂时无法读取，可能遗漏最新动态。");
  return notices;
}
export function Crest({
  team,
  name,
  small = false,
}: {
  team?: Team;
  name?: string;
  small?: boolean;
}) {
  const [failed, setFailed] = useState(false);
  const label = team?.name || name || "球队";
  const key =
    `${team?.id || ""} ${team?.english_name || ""} ${label}`.toLowerCase();
  const fallbackAsset = /arsenal|阿森纳/.test(key)
    ? "arsenal.svg"
    : /liverpool|利物浦/.test(key)
      ? "liverpool.png"
      : /barcelona|巴塞罗那|巴萨/.test(key)
        ? "barcelona.png"
        : /(^| )inter( |$)|internazionale|国际米兰|国米/.test(key)
          ? "inter.png"
          : null;
  const configuredAsset = team?.crest_url;
  const asset = configuredAsset?.startsWith("/assets/")
    ? configuredAsset
    : safeUrl(configuredAsset) || (fallbackAsset ? `/assets/${fallbackAsset}` : null);
  useEffect(() => setFailed(false), [asset]);
  return (
    <span
      className={`crest ${small ? "small" : ""} ${asset && !failed ? "crest-image" : "crest-fallback"}`}
      style={{ background: team?.color || "#758b79" }}
      title={`${label}${asset && !failed ? "队徽" : " · 暂无队徽"}`}
    >
      {asset && !failed ? (
        <img
          src={asset}
          alt={`${label}队徽`}
          onError={() => setFailed(true)}
        />
      ) : (
        (team?.name || name || team?.short_name || team?.english_name || "队")
          .slice(0, 1)
          .toUpperCase()
      )}
    </span>
  );
}
export function Empty({
  title,
  children,
  action,
}: {
  title: string;
  children?: ReactNode;
  action?: ReactNode;
}) {
  return (
    <div className="empty-state">
      <div className="empty-icon">
        <BookOpen size={25} strokeWidth={1.5} />
      </div>
      <h3>{title}</h3>
      {children && <p>{children}</p>}
      {action && <div className="empty-action">{action}</div>}
    </div>
  );
}
export function ErrorNotice({ children }: { children?: ReactNode }) {
  return children ? (
    <div className="error-notice" role="alert">
      <CircleAlert size={18} />
      <span>{children}</span>
    </div>
  ) : null;
}
export function Spinner({ text = "正在加载" }: { text?: string }) {
  return (
    <span className="loading-label">
      <LoaderCircle className="spin" size={17} />
      {text}
    </span>
  );
}
const modalStack: symbol[] = [];
let originalBodyOverflow = "";
export function Modal({
  title,
  children,
  onClose,
  wide = false,
}: {
  title: string;
  children: ReactNode;
  onClose: () => void;
  wide?: boolean;
}) {
  const panel = useRef<HTMLDivElement>(null);
  const close = useRef(onClose);
  close.current = onClose;
  useEffect(() => {
    const token = Symbol("modal");
    const before = document.activeElement as HTMLElement | null;
    if (!modalStack.length) originalBodyOverflow = document.body.style.overflow;
    modalStack.push(token);
    document.body.style.overflow = "hidden";
    panel.current?.focus();
    const key = (event: KeyboardEvent) => {
      if (modalStack[modalStack.length - 1] !== token) return;
      if (event.key === "Escape") {
        event.preventDefault();
        close.current();
      }
      if (event.key === "Tab") {
        const nodes = Array.from(
          panel.current?.querySelectorAll<HTMLElement>(
            'button:not([disabled]), a[href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex="0"]',
          ) || [],
        ).filter((node) => node.getClientRects().length > 0);
        const first = nodes[0],
          last = nodes[nodes.length - 1];
        if (!first) {
          event.preventDefault();
          return;
        }
        if (
          event.shiftKey &&
          (document.activeElement === first ||
            document.activeElement === panel.current)
        ) {
          event.preventDefault();
          last.focus();
        } else if (
          !event.shiftKey &&
          (document.activeElement === last ||
            document.activeElement === panel.current)
        ) {
          event.preventDefault();
          first.focus();
        }
      }
    };
    document.addEventListener("keydown", key);
    return () => {
      const wasTop = modalStack[modalStack.length - 1] === token;
      const index = modalStack.indexOf(token);
      if (index !== -1) modalStack.splice(index, 1);
      if (!modalStack.length) document.body.style.overflow = originalBodyOverflow;
      document.removeEventListener("keydown", key);
      if (wasTop && before?.isConnected) before.focus();
    };
  }, []);
  return (
    <div
      className="modal-backdrop"
      onMouseDown={(e) => e.target === e.currentTarget && onClose()}
    >
      <div
        className={`modal-panel ${wide ? "wide" : ""}`}
        ref={panel}
        role="dialog"
        aria-modal="true"
        aria-label={title}
        tabIndex={-1}
      >
        <div className="modal-heading">
          <div>
            <span className="eyebrow">相关信息</span>
            <h2>{title}</h2>
          </div>
          <button
            className="icon-button"
            onClick={onClose}
            aria-label="关闭详情"
          >
            <X size={21} />
          </button>
        </div>
        {children}
      </div>
    </div>
  );
}
export function EvidenceDetail({ item }: { item: Evidence }) {
  const url = safeUrl(item.url);
  return (
    <article className="evidence-detail">
      <div className="meta">
        <span className="tag">
          {item.mode === "demo"
            ? "虚构示例"
            : item.origin === "manual"
              ? "手动导入"
              : "参考来源"}
        </span>
        <span>{sourceLabel(item)}</span>
      </div>
      <h3>{evidenceTitle(item)}</h3>
      {(item.spoiler_redacted || item.spoiler_hidden) && (
        <div className="warning-notice">
          已隐藏赛果信息，原网页可能包含比赛结果。
        </div>
      )}
      <dl className="detail-metadata">
        <div>
          <dt>发布时间</dt>
          <dd>
            {item.published_at ? dateTime(item.published_at) : "发布时间未知"}
          </dd>
        </div>
      </dl>
      {url && (
        <a
          className="source-link"
          href={url}
          target="_blank"
          rel="noopener noreferrer"
        >
          查看来源 <ArrowUpRight size={15} />
        </a>
      )}
      {!url && <p className="fine-print">这份本地资料没有附带来源链接。</p>}
    </article>
  );
}
export function RunView({
  run,
  evidence,
  openEvidence,
  onCancel,
  onRetry,
  onSettings,
  onResume,
  cancelling,
  compact = false,
}: {
  run: Run;
  evidence: Evidence[];
  openEvidence: (item: Evidence) => void;
  onCancel: () => void;
  onRetry: () => void;
  onSettings: () => void;
  onResume?: () => void;
  cancelling: boolean;
  compact?: boolean;
}) {
  const [expanded, setExpanded] = useState(false);
  useEffect(() => {
    setExpanded(false);
  }, [run.id]);
  const active = run.status === "queued" || run.status === "running";
  const allEvidence = [...(run.evidence || []), ...evidence].filter(
    (item) => item.mode === run.mode && item.team_id === run.team_id,
  );
  const byId = new Map(allEvidence.map((item) => [item.id, item]));
  const validIds = [...new Set(run.evidence_ids || [])];
  const answer = (run.answer || "").replace(
    /\[([^\]\n]+)\]/g,
    (text, id: string) =>
      validIds.includes(id)
        ? `[${validIds.indexOf(id) + 1}](#evidence-${encodeURIComponent(id)})`
        : text,
  );
  return (
    <article className={`run-card ${compact ? "brief-card" : ""}`}>
      <div className="run-heading">
        <div className="run-label">
          {active ? (
            <LoaderCircle size={17} className="spin" />
          ) : run.status === "completed" ? (
            <CircleCheck size={17} />
          ) : (
            <CircleAlert size={17} />
          )}
          <strong>{statusNames[run.status] || "处理中"}</strong>
        </div>
        {active && (
          <button
            className="button compact"
            disabled={cancelling}
            onClick={onCancel}
          >
            <Square size={13} />
            {cancelling ? "停止中" : "停止"}
          </button>
        )}
      </div>
      {run.mode === "demo" && !run.answer && (
        <div className="demo-inline">虚构示例</div>
      )}
      <h3 className="run-question">
        {compact ? "球队近况解读" : run.question}
      </h3>
      {run.answer ? (
        <div
          className={`answer-text ${compact && !expanded ? "brief-answer" : ""}`}
        >
          <ReactMarkdown
            remarkPlugins={[remarkGfm]}
            components={{
              img: ({ alt }) => <span>{alt || ""}</span>,
              a: ({ href, children }) => {
                if (href?.startsWith("#evidence-")) {
                  const id = validIds.find((value) => `#evidence-${encodeURIComponent(value)}` === href);
                  const item = id ? byId.get(id) : undefined;
                  const url = safeUrl(item?.url);
                  if (item && url) return (
                    <a className="citation" href={url} target="_blank" rel="noopener noreferrer" title={evidenceTitle(item)}>
                      {children}
                    </a>
                  );
                  return item ? (
                    <button
                      className="citation"
                      onClick={() => openEvidence(item)}
                      title={evidenceTitle(item)}
                    >
                      {children}
                    </button>
                  ) : (
                    <span>{children}</span>
                  );
                }
                const url = safeUrl(href);
                return url ? (
                  <a href={url} target="_blank" rel="noopener noreferrer">
                    查看来源
                  </a>
                ) : (
                  <span>{children}</span>
                );
              },
            }}
          >
            {answer}
          </ReactMarkdown>
        </div>
      ) : active ? (
        <div className="run-pending">
          <span className="pulse-dot" />
          正在查阅相关消息…
        </div>
      ) : (
        <p className="muted">本次没有生成完整回答。</p>
      )}
      {compact && run.answer && (
        <button
          className="text-button brief-expand"
          onClick={() => setExpanded(!expanded)}
        >
          {expanded ? "收起解读" : "展开解读"}
          <ChevronDown size={14} />
        </button>
      )}
      {run.status !== "cancelled" && <ErrorNotice>{runErrorMessage(run.error)}</ErrorNotice>}
      {answerNotices(run).map((warning, index) => (
        <div className="warning-notice" key={index}>
          <CircleAlert size={16} />
          <span>{warning}</span>
        </div>
      ))}
      {validIds.length > 0 && (
        <details className="run-evidence">
          <summary className="section-caption">
            <BookOpen size={15} />
            参考来源 <span>{validIds.length}</span>
            <ChevronDown size={14} />
          </summary>
          {validIds.map((id, index) => {
            const item = byId.get(id);
            const url = safeUrl(item?.url);
            const content = item ? (
              <>
                <span className="evidence-number">{index + 1}</span>
                <span>
                  <strong>{evidenceTitle(item)}</strong>
                  <small>
                    {sourceLabel(item)} ·{" "}
                    {item.published_at
                      ? dateTime(item.published_at)
                      : "发布时间未知"}
                  </small>
                </span>
                <ArrowUpRight size={16} />
              </>
            ) : null;
            return item ? (
              url ? (
                <a key={id} className="evidence-row" href={url} target="_blank" rel="noopener noreferrer">
                  {content}
                </a>
              ) : (
                <button key={id} className="evidence-row" onClick={() => openEvidence(item)}>
                  {content}
                </button>
              )
            ) : (
              <p className="fine-print" key={id}>
                来源 {index + 1} 暂不可用。
              </p>
            );
          })}
        </details>
      )}
      <footer className="run-footer">
        <span>{dateTime(run.created_at)}</span>
        {!active && (
          <div className="button-group">
            {onResume &&
              (run.status === "failed" || run.status === "cancelled") && (
                <button className="text-button" onClick={onResume}>
                  继续查询
                </button>
              )}
            <button className="text-button" onClick={onRetry}>
              重新回答
            </button>
            {run.status === "failed" && (
              <button className="text-button" onClick={onSettings}>
                助手设置
              </button>
            )}
          </div>
        )}
      </footer>
    </article>
  );
}
