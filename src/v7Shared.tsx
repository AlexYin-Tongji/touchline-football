import { memo, useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import { ArrowUpRight, ImageOff } from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { safeUrl } from "./components";
import type { Evidence } from "./types";

// Stable across renders: react-markdown keys its parse cache on these
// references, so an inline array/object here would invalidate the cache on
// every render and re-parse every card in the list on each poll.
const remarkPlugins = [remarkGfm];
const markdownComponents = {
  a: ({ href, children }: { href?: string; children?: ReactNode }) =>
    safeUrl(href) ? (
      <a href={safeUrl(href)!} target="_blank" rel="noopener noreferrer">
        {children}
      </a>
    ) : (
      <span>{children}</span>
    ),
  // Images are rendered separately from verified source metadata.
  img: () => null,
};

export interface SourceLink {
  id?: string;
  name?: string;
  source_name?: string;
  title?: string;
  url?: string;
  published_at?: string | null;
}
export interface MediaImage {
  url: string;
  alt?: string;
  credit?: string;
  source_url?: string;
  caption?: string;
}
export function uniqueImages(
  images: MediaImage[] = [],
  seen = new Set<string>(),
) {
  return images.filter((image) => {
    if (!safeUrl(image.source_url)) return false;
    const safe = image.url?.startsWith("/assets/")
      ? image.url
      : safeUrl(image.url);
    if (!safe) return false;
    const identity = safe.split(/[?#]/)[0];
    if (seen.has(identity)) return false;
    seen.add(identity);
    return true;
  });
}
export const Photo = memo(function Photo({
  image,
  className = "",
  placeholder = false,
}: {
  image?: MediaImage | null;
  className?: string;
  placeholder?: boolean;
}) {
  const [failed, setFailed] = useState(false);
  useEffect(() => setFailed(false), [image?.url]);
  const url = image?.url?.startsWith("/assets/")
    ? image.url
    : safeUrl(image?.url);
  if (!url || failed)
    return placeholder ? (
      <div className={`photo-placeholder ${className}`}>
        <ImageOff size={23} />
        <span>暂无照片</span>
      </div>
    ) : null;
  return (
    <figure className={`photo ${className}`}>
      <img
        src={url}
        alt={image?.alt || "球队相关图片"}
        loading="lazy"
        decoding="async"
        referrerPolicy="no-referrer"
        onError={() => setFailed(true)}
      />
      {(image?.credit || image?.caption || image?.source_url) && (
        <figcaption>
          {image.caption || image.credit || "查看图片来源"}
          {image.source_url && safeUrl(image.source_url) && (
            <a
              href={safeUrl(image.source_url)!}
              target="_blank"
              rel="noopener noreferrer"
              aria-label="图片来源"
            >
              <ArrowUpRight size={12} />
            </a>
          )}
        </figcaption>
      )}
    </figure>
  );
});
export const Sources = memo(function Sources({
  sources = [],
}: {
  sources?: SourceLink[];
}) {
  const unique = sources.filter(
    (source, i, all) =>
      safeUrl(source.url) &&
      all.findIndex((other) => other.url === source.url) === i,
  );
  if (!unique.length) return null;
  return (
    <div className="source-links">
      <span>来源</span>
      {unique.map((source, i) => (
        <a
          key={source.url}
          href={safeUrl(source.url)!}
          target="_blank"
          rel="noopener noreferrer"
          title={source.title}
        >
          {source.name || source.source_name || `参考 ${i + 1}`}
          <ArrowUpRight size={12} />
        </a>
      ))}
    </div>
  );
});
export const Markdown = memo(function Markdown({
  text,
  evidence = [],
}: {
  text: string;
  evidence?: (SourceLink | Evidence)[];
}) {
  const content = useMemo(
    () =>
      text.replace(/\[([^\]\n]+)\]/g, (original, id: string) => {
        const index = evidence.findIndex((item) => item.id === id);
        const item = evidence[index];
        return item && safeUrl(item.url)
          ? `[${index + 1}](${safeUrl(item.url)})`
          : original;
      }),
    [text, evidence],
  );
  return (
    <div className="markdown">
      <ReactMarkdown remarkPlugins={remarkPlugins} components={markdownComponents}>
        {content}
      </ReactMarkdown>
    </div>
  );
});
export function friendlyTime(value?: string | null) {
  if (!value || !Number.isFinite(Date.parse(value))) return "";
  const minutes = Math.floor((Date.now() - Date.parse(value)) / 60000);
  if (minutes >= 0 && minutes < 1) return "刚刚";
  if (minutes > 0 && minutes < 60) return `${minutes} 分钟前`;
  if (minutes >= 60 && minutes < 1440)
    return `${Math.floor(minutes / 60)} 小时前`;
  return new Date(value).toLocaleDateString("zh-CN", {
    month: "long",
    day: "numeric",
  });
}
