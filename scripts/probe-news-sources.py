"""Find public entry points on football sites this network can actually reach.

The reviewed publisher list is entirely Western European and US, and on a
network whose DNS is intercepted almost none of it resolves. The obvious fix —
paste six well-known Chinese sites into `news_sources.py` — is the one this
project has already rejected twice: `espn_soccer` answered 202 with a zero-byte
body and `gazzetta_football` served an index frozen at 2026-03-31, and both were
removed rather than left occupying a source slot.

So nothing is declared until it has been fetched and found to yield entries.
This script does the finding. It is a probe, not an importer: it prints an
evidence table, and only `--write` records the entries it actually reached.

RSS is not assumed. Most Chinese sports sites retired their feeds years ago —
`sports.sina.com.cn/roll/rss/sports.xml` and `www.zhibo8.com/rss` both answer
404 today. Each candidate is therefore tried against several entry shapes, and
the shapes the running app already knows how to read are tried first: a declared
news API (`_declared_news_api`), an advertised index, a sitemap, then RSS.

Usage:
    python -I scripts/probe-news-sources.py              # probe, print evidence
    python -I scripts/probe-news-sources.py --write      # record what worked
    python -I scripts/probe-news-sources.py --site hupu  # one candidate
"""
from __future__ import annotations

import argparse
import io
import json
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from server import ingestion  # noqa: E402
from server import research  # noqa: E402

CHANNELS = ROOT / "public/assets/clubs/news_channels.json"
REVIEWED_AT = time.strftime("%Y-%m-%d")

# Candidate entry shapes per site, most likely to work first. `kind` is what the
# app would use if the shape is adopted: `rss` for a feed, `html` for an index
# page the reader parses, `json` for a declared API returning article lists.
CANDIDATES = {
    "dongqiudi": {
        "name": "懂球帝",
        "hosts": ["www.dongqiudi.com", "api.dongqiudi.com"],
        "shapes": [("json", "https://www.dongqiudi.com/api/app/tabs/web/1.json"),
                   ("rss", "https://www.dongqiudi.com/rss"),
                   ("html", "https://www.dongqiudi.com/")],
    },
    "zhibo8": {
        "name": "直播吧",
        "hosts": ["www.zhibo8.com", "news.zhibo8.com"],
        "shapes": [("rss", "https://www.zhibo8.com/rss.xml"),
                   ("html", "https://news.zhibo8.com/")],
    },
    "sina_sports": {
        "name": "新浪体育",
        "hosts": ["sports.sina.com.cn"],
        "shapes": [("rss", "https://rss.sina.com.cn/sports/global.xml"),
                   ("html", "https://sports.sina.com.cn/")],
    },
    "qq_sports": {
        "name": "腾讯体育",
        "hosts": ["sports.qq.com"],
        "shapes": [("rss", "https://sports.qq.com/rss/"),
                   ("html", "https://sports.qq.com/")],
    },
    "hupu": {
        "name": "虎扑",
        "hosts": ["www.hupu.com", "bbs.hupu.com"],
        "shapes": [("rss", "https://www.hupu.com/rss"),
                   ("html", "https://www.hupu.com/")],
    },
    "netease_sports": {
        "name": "网易体育",
        "hosts": ["sports.163.com", "www.163.com"],
        # The article pages live on www.163.com while every section index lives
        # on sports.163.com, so both hosts belong to every shape.
        "shapes": [("html", "https://sports.163.com/gjb/"),
                   ("html", "https://sports.163.com/yc/"),
                   ("html", "https://sports.163.com/"),
                   ("rss", "https://sports.163.com/special/")],
    },
}


def use_utf8_stdout():
    """Make Chinese site names survive a Windows console.

    Done in the entry point rather than at import: the module is imported by
    tests, and replacing `sys.stdout` there would detach the runner's summary.
    """
    if hasattr(sys.stdout, "buffer"):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")


def probe_shape(kind, url, hosts):
    """Fetch one entry shape and count what the running reader could use.

    ``hosts`` must list every host the shape needs: a Chinese portal commonly
    serves its section index from one hostname and its articles from another,
    so a probe that checked only the index host would reject a shape the app
    can read.
    """
    allow = set(hosts) | {urlsplit(url).hostname}
    try:
        body, final_url, content_type = ingestion.fetch_document(
            url, allowed_hosts=allow, max_body=ingestion.MAX_BODY,
            content_types=("text/", "xml", "xhtml", "json") if kind == "json" else ("text/", "xml", "xhtml"))
    except Exception as exc:  # a probe reports failure; it never raises on one
        return {"reachable": False, "error": ingestion.source_error_details(exc)["message"]}
    entries = []
    # A source dict shaped the way the reader builds one, so the parsers apply
    # their real host and article-path rules instead of a loosened test version.
    source = {"id": "probe_" + url, "name": "probe", "url": url, "builtin": False,
              "allowed_hosts": sorted(allow)}
    if kind == "rss":
        try:
            entries = research._feed_candidates(body, final_url)
        except Exception:
            entries = []
    elif kind == "html":
        # Reuse the reader's own extraction so a shape is only adopted when the
        # app can actually read it, not when a regex happens to match.
        try:
            entries = research._html_candidates(body, final_url, source)
        except Exception:
            entries = []
        if not entries:
            try:
                entries = research._sitemap_candidates(body)
            except Exception:
                entries = []
    return {"reachable": True, "final_url": final_url, "content_type": content_type,
            "bytes": len(body), "entries": len(entries),
            "sample": [entry.get("url") for entry in entries[:3]]}


def main(args):
    use_utf8_stdout()
    selected = args.site.split(",") if args.site else list(CANDIDATES)
    unknown = [name for name in selected if name not in CANDIDATES]
    if unknown:
        raise SystemExit("unknown site ids: " + ", ".join(unknown))

    report, adopted = {}, []
    for site_id in selected:
        candidate = CANDIDATES[site_id]
        print(f"\n== {candidate['name']} ({site_id}) ==", flush=True)
        best = None
        for kind, url in candidate["shapes"]:
            result = probe_shape(kind, url, candidate["hosts"])
            if result["reachable"]:
                print(f"  {kind:5s} {url}\n        -> HTTP ok, {result['bytes']} bytes, "
                      f"{result['entries']} entries", flush=True)
            else:
                print(f"  {kind:5s} {url}\n        -> {result['error']}", flush=True)
            report.setdefault(site_id, {})[kind] = result
            # Adopt the first shape that yields entries: a reachable page with
            # nothing parseable is exactly the failure mode this guards against.
            if best is None and result.get("entries"):
                best = {"kind": kind, "url": url, "entries": result["entries"]}
        if best:
            adopted.append({"id": f"domestic_{site_id}", "name": candidate["name"],
                            "kind": best["kind"], "url": best["url"],
                            "allowed_hosts": sorted(candidate["hosts"]),
                            "declaration_url": best["url"], "reviewed_at": REVIEWED_AT,
                            "description": f"{candidate['name']}公开栏目；按当前球队与男足一线队范围筛选。"})
            print(f"  => 采用 {best['kind']} {best['url']}（{best['entries']} 条）", flush=True)
        else:
            print("  => 未发现可用入口，不声明", flush=True)

    if args.write:
        payload = {"reviewed_at": REVIEWED_AT,
                   "note": "只收录本机实测可读到条目的入口；未通过探测的站点不写入。",
                   "channels": adopted}
        CHANNELS.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"\n已写入 {CHANNELS.relative_to(ROOT)}（{len(adopted)} 个来源）", flush=True)
    else:
        print(f"\n计划：{len(adopted)} 个来源可用。加 --write 记录。", flush=True)
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="record the reachable entries")
    parser.add_argument("--site", help="comma-separated candidate ids")
    raise SystemExit(main(parser.parse_args()))
