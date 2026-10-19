import { useState } from "react";
import { ArrowRight } from "lucide-react";

const VERSION = "9.3.0";

/**
 * Opening screen.
 *
 * The app has no login and no onboarding — it opens directly into a club's
 * feed. On first paint, before anything is loaded, that is disorienting: an
 * empty shell with a sidebar and three vague tabs. This screen states what
 * the product is and gives one way in. It appears once per page load.
 */
export function Cover({ onEnter }: { onEnter: () => void }) {
  const [leaving, setLeaving] = useState(false);

  const enter = () => {
    if (leaving) return;
    setLeaving(true);
    // Let the fade finish before the app mounts behind it. The timeout is a
    // fallback in case transitionend never fires (reduced motion, background tab).
    window.setTimeout(onEnter, 260);
  };

  return (
    <div
      className={`cover${leaving ? " is-leaving" : ""}`}
      role="dialog"
      aria-modal="true"
      aria-label="看台"
      onKeyDown={(event) => {
        if (event.key === "Enter" || event.key === " " || event.key === "Escape") {
          event.preventDefault();
          enter();
        }
      }}
    >
      <div className="cover-art" aria-hidden="true" />
      <div className="cover-inner">
        <div className="cover-brand">
          <span className="cover-mark">
            <svg width="30" height="30" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
              <circle cx="12" cy="12" r="9" />
              <path d="M12 7v5l3 3" />
            </svg>
          </span>
          <span className="cover-name">看台</span>
          <span className="cover-latin">TOUCHLINE</span>
        </div>

        <h1 className="cover-title">
          你的私人
          <br />
          足球编辑部
        </h1>
        <p className="cover-lead">
          一支球队的动态、阵容、赛程与数据，
          <br className="desktop-only" />
          整理成中文，存在你自己的电脑里。
        </p>

        <button type="button" className="cover-enter" onClick={enter} autoFocus>
          进入看台
          <ArrowRight size={17} aria-hidden="true" />
        </button>

        <p className="cover-foot">
          本地运行 · 数据不出本机
          <span aria-hidden="true">·</span>
          <span className="cover-version">V{VERSION}</span>
        </p>
      </div>
    </div>
  );
}
