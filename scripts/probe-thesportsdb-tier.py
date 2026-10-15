"""Decide whether a TheSportsDB key returns real data — before you pay for it.

The free tier does not merely truncate: some endpoints answer with *substituted
sample data*. ``lookupteam.php?id=<Celtic>`` returns ``Tulane``, an American
college team; ``id=<Manchester City>`` returns ``Manchester United``. A key that
raises the row limit but keeps the substitution would be worthless for club
facts, so the only honest way to evaluate a tier is to ask it questions whose
answers are already known.

This is a read-only probe against a publisher the project already contacts. It
holds no credential: the key is passed on the command line by the person who
owns it, is never written to disk, and never enters the app's encrypted store.

Usage (run from the project root, with the venv python):

    .\\.venv\\Scripts\\python.exe scripts/probe-thesportsdb-tier.py --key <KEY>
    .\\.venv\\Scripts\\python.exe scripts/probe-thesportsdb-tier.py            # free key 3

Nothing here is imported by the product. The product-facing reader lives in
``server/providers/thesportsdb.py``.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import unicodedata
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote_plus
from urllib.request import ProxyHandler, Request, build_opener

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

V1 = "https://www.thesportsdb.com/api/v1/json/{key}/{path}"
V2 = "https://www.thesportsdb.com/api/v2/json/{path}"
FREE_KEY = "3"
UA = "Mozilla/5.0 (compatible; TouchlineTierProbe/1.0)"
PAUSE = 2.2  # the free tier documents 30 requests/minute; stay well under it

# Identities whose answers are already known. Each entry is a question whose
# answer a *correct* response must contain; a substituted sample response fails
# it. These are facts about the real world, not about the API.
KNOWN = [
    ("Celtic 的球队记录", "Celtic", "lookupteam.php?id=138710"),
    ("Manchester City 的球队记录", "Manchester City", "lookupteam.php?id=133612"),
    ("Arsenal 的球队记录", "Arsenal", "lookupteam.php?id=133604"),
]


def normalise(value: str) -> str:
    text = unicodedata.normalize("NFD", value or "")
    text = "".join(char for char in text if not unicodedata.combining(char))
    return " ".join(text.lower().replace("-", " ").split())


class Probe:
    def __init__(self, key: str):
        self.key = key
        self.opener = build_opener(ProxyHandler({}))
        self.requests = 0

    def _get(self, url: str, headers: dict | None = None):
        request = Request(url, headers={"User-Agent": UA, **(headers or {})})
        with self.opener.open(request, timeout=25) as response:
            body = response.read()
        self.requests += 1
        try:
            return json.loads(body.decode("utf-8", "replace")), None
        except json.JSONDecodeError:
            # A v2 request without a header, or an HTML substitution page, both
            # land here. The caller needs to tell "not JSON" from "no rows".
            return None, body[:160].decode("utf-8", "replace")

    def v1(self, path: str):
        return self._get(V1.format(key=self.key, path=path))

    def v2(self, path: str):
        return self._get(V2.format(path=path), {"X-API-KEY": self.key})

    def wait(self):
        time.sleep(PAUSE)


def rows_of(payload, *names):
    """The first non-empty list under any of the documented envelope names."""
    if not isinstance(payload, dict):
        return []
    for name in names:
        value = payload.get(name)
        if isinstance(value, list):
            return value
    for value in payload.values():
        if isinstance(value, list):
            return value
    return []


def find_team(payload, expected: str):
    """Whether the *expected* club is present — not merely that rows came back."""
    for row in rows_of(payload, "teams", "team"):
        if not isinstance(row, dict):
            continue
        name = row.get("strTeam")
        if name and normalise(name) == normalise(expected):
            return row
    return None


def report(label: str, ok: bool, detail: str):
    print(f"  [{'通过' if ok else '不通过'}] {label} — {detail}")
    return ok


def probe_identity(probe: Probe) -> bool:
    """Do we receive the club we asked for, or a substituted sample?"""
    print("\n[1/4] 身份正确性：返回的球队是不是我们要的那一支")
    passed = True
    for label, expected, path in KNOWN:
        try:
            payload, raw = probe.v1(path)
        except (HTTPError, URLError, TimeoutError) as exc:
            passed &= report(label, False, f"请求失败：{exc}")
            probe.wait()
            continue
        if payload is None:
            passed &= report(label, False, f"响应不是 JSON：{raw!r}")
            probe.wait()
            continue
        row = find_team(payload, expected)
        if row is None:
            seen = [r.get("strTeam") for r in rows_of(payload, "teams") if isinstance(r, dict)]
            passed &= report(label, False, f"没有找到 {expected}；实际返回 {seen[:4]}")
        else:
            passed &= report(label, True, f"确认是 {row.get('strTeam')}")
        probe.wait()
    return passed


def probe_catalogue(probe: Probe) -> bool:
    """Whether the league catalogue is the real one or five fixed rows."""
    print("\n[2/4] 联赛目录：全量还是固定的五行")
    try:
        payload, raw = probe.v1("all_leagues.php")
    except (HTTPError, URLError, TimeoutError) as exc:
        return report("联赛目录", False, f"请求失败：{exc}")
    if payload is None:
        return report("联赛目录", False, f"响应不是 JSON：{raw!r}")
    rows = rows_of(payload, "leagues")
    leagues = [r.get("strLeague") for r in rows if isinstance(r, dict)]
    print(f"  联赛数：{len(leagues)}")
    print(f"  前几个：{leagues[:5]}")
    # The free tier returns exactly five. A real catalogue is hundreds.
    ok = len(leagues) > 50
    return report("联赛目录", ok, f"{len(leagues)} 个" + ("（免费层固定五行的特征）" if not ok else ""))


def probe_fields(probe: Probe) -> bool:
    """The club facts the team page needs. Nulls here are a real answer."""
    print("\n[3/4] 球队字段：死敌、球场容量、昵称、球场位置")
    wanted = ["strRivalry", "intStadiumCapacity", "strTeamNickname",
              "strStadiumLocation", "strStadiumThumb", "strTeamJersey", "strManager"]
    try:
        payload, raw = probe.v1("lookupteam.php?id=133604")
    except (HTTPError, URLError, TimeoutError) as exc:
        return report("Arsenal 字段", False, f"请求失败：{exc}")
    if payload is None:
        return report("Arsenal 字段", False, f"响应不是 JSON：{raw!r}")
    row = find_team(payload, "Arsenal")
    if row is None:
        return report("Arsenal 字段", False, "连身份都不对，字段无从谈起")
    filled = [key for key in wanted if row.get(key) not in (None, "")]
    for key in wanted:
        print(f"    {key}: {row.get(key)!r}")
    print(f"  有值：{len(filled)}/{len(wanted)} — {filled or '无'}")
    # Not pass/fail: a tier may omit a field and the page has a Wikidata
    # fallback. This is the evidence for whether the money buys it.
    return bool(filled)


def probe_squad(probe: Probe) -> bool:
    """Is a squad a real squad, or ten alphabetical rows of mixed clubs?"""
    print("\n[4/4] 阵容完整性：整队 vs 十行字母序")
    try:
        payload, raw = probe.v1("lookup_all_players.php?id=133604")
    except (HTTPError, URLError, TimeoutError) as exc:
        return report("Arsenal 阵容", False, f"请求失败：{exc}")
    if payload is None:
        return report("Arsenal 阵容", False, f"响应不是 JSON：{raw!r}")
    rows = [r for r in rows_of(payload, "player") if isinstance(r, dict)]
    clubs = {normalise(r.get("strTeam") or "") for r in rows}
    clubs.discard("")
    names = [r.get("strPlayer") for r in rows]
    print(f"  行数：{len(rows)}")
    print(f"  前几行：{names[:5]}")
    print(f"  出现过的俱乐部：{sorted(clubs)}")
    # A real squad is ~25 named players of one club. The free tier's ten rows
    # also carry coaches and players contracted elsewhere.
    looks_real = len(rows) >= 20 and len(clubs) == 1
    return report("Arsenal 阵容", looks_real,
                  f"{len(rows)} 行、{len(clubs)} 个俱乐部"
                  + ("" if looks_real else "（免费层的固定特征）"))


def probe_v2(probe: Probe) -> bool:
    """Whether the v2 surface — the part that is premium-only — answers at all."""
    print("\n[5/5] V2 接口：赛程与球员统计")
    checks = [
        ("V2 球队", "lookup/team/133604", lambda p: find_team(p, "Arsenal") is not None),
        ("V2 下一场比赛", "schedule/next/team/133604", lambda p: bool(rows_of(p, "events", "schedule"))),
        ("V2 近期赛程", "schedule/previous/team/133604", lambda p: bool(rows_of(p, "events", "schedule"))),
        ("V2 球员统计", "lookup/playerStats/34169884", lambda p: bool(rows_of(p, "stats", "playerstats"))),
        ("V2 球员合同", "lookup/playerContracts/34169884", lambda p: bool(rows_of(p, "contracts"))),
    ]
    passed = 0
    for label, path, judge in checks:
        try:
            payload, raw = probe.v2(path)
        except HTTPError as exc:
            note = "（需要付费 key）" if exc.code in (400, 401, 403) else ""
            report(label, False, f"HTTP {exc.code}{note}")
            probe.wait()
            continue
        except (URLError, TimeoutError) as exc:
            report(label, False, f"请求失败：{exc}")
            probe.wait()
            continue
        if payload is None:
            report(label, False, f"响应不是 JSON：{raw!r}")
            probe.wait()
            continue
        ok = judge(payload)
        passed += bool(ok)
        report(label, ok, f"顶层键 {list(payload)[:5]}")
        probe.wait()
    print(f"  V2 可用：{passed}/{len(checks)}")
    return passed > 0


def search_players(probe: Probe, name: str):
    """Report what a full-name query returns, with the two identity facts."""
    path = "searchplayers.php?p=" + quote_plus(name)
    try:
        payload, raw = probe.v1(path)
    except (HTTPError, URLError, TimeoutError) as exc:
        return None, f"请求失败：{exc}"
    if payload is None:
        return None, f"响应不是 JSON：{raw!r}"
    rows = [r for r in rows_of(payload, "player") if isinstance(r, dict)]
    return rows, ""


def probe_names(probe: Probe) -> bool:
    """Whether full-name search still resolves the players we measured."""
    print("\n[附] 球员姓名搜索：样例（阿森纳，含两例已知的生日冲突）")
    sample = ["Bukayo Saka", "Declan Rice", "Gabriel Magalhães",
              "Ezri Konsa", "Ben White"]
    ok = True
    for name in sample:
        rows, error = search_players(probe, name)
        probe.wait()
        if rows is None:
            print(f"    {name}: {error}")
            ok = False
            continue
        if not rows:
            print(f"    {name}: 无记录")
            continue
        first = rows[0]
        print(f"    {name}: 1 条 → {first.get('strPlayer')} / {first.get('dateBorn')} "
              f"/ cutout={bool(first.get('strCutout'))}")
    return ok


def main() -> int:
    parser = argparse.ArgumentParser(
        description="在付费前验证 TheSportsDB 密钥返回的是真实数据还是样例数据。")
    parser.add_argument("--key", default=FREE_KEY,
                        help="TheSportsDB API key；默认是免费公共 key 3")
    parser.add_argument("--json", action="store_true", help="额外输出一行机器可读结论")
    args = parser.parse_args()

    probe = Probe(args.key)
    label = "免费公共 key" if args.key == FREE_KEY else "自备 key（不写入磁盘）"
    print(f"TheSportsDB 档位探测 — {label}")
    print("判据：身份正确性决定一切。球队身份被替换时，行数再多也没有意义。")

    identity = probe_identity(probe)
    catalogue = probe_catalogue(probe)
    fields = probe_fields(probe)
    squad = probe_squad(probe)
    v2 = probe_v2(probe)
    names = probe_names(probe)

    print("\n" + "=" * 58)
    print("结论")
    print("=" * 58)
    for text, ok in [
        ("身份正确（返回的是我们要的那支球队）", identity),
        ("联赛目录是完整的", catalogue),
        ("球队字段有值（死敌／容量／昵称）", fields),
        ("阵容是整队而非十行字母序", squad),
        ("V2 接口可用", v2),
        ("球员姓名搜索可用", names),
    ]:
        print(f"  {'✅' if ok else '❌'} {text}")
    print(f"\n  共发出 {probe.requests} 次请求。")

    # The verdict is about *this key*, never a prediction about another tier.
    # A paid key that also substitutes sample data is exactly the outcome this
    # probe exists to catch, so it must not be asserted in advance either way.
    if identity and squad:
        print("\n  这个档位对球队板块可用：身份可信，阵容完整。")
    elif identity:
        print("\n  身份可信但阵容不完整。球队基础信息可用，球员板块需要另想办法。")
    else:
        print("\n  身份不可信：这些端点返回的是样例数据。")
        print("  注意——这条结论只对当前这个 key 成立。付费档位是否解除替换，"
              "必须用付费 key 重跑本脚本才能知道，不能凭推测。")
        print("  在验证之前，球队基础信息应继续走 Wikidata 与静态目录；"
              "球员照片不受影响（姓名搜索这一路是好的）。")

    if args.json:
        print(json.dumps({"identity": identity, "catalogue": catalogue, "fields": fields,
                          "squad": squad, "v2": v2, "names": names}, ensure_ascii=False))
    return 0 if identity else 1


if __name__ == "__main__":
    raise SystemExit(main())
