"""Panel workflow for team-center refresh.

The top-level skill is ``team``. It reads one gap snapshot and dispatches the
six leaves from the panel skill: ``club.crest``, ``squad.info``,
``squad.names``, ``squad.images``, ``schedule.standings``, then ``stats.charts``.
Crest, roster, photos and the schedule may run together. Chinese names wait
until every roster row has a ``provider_id``. When this round rewrites the
roster, names run after that write and may then run beside photos. Charts run
only after this round's schedule attempt, or from a schedule cache that is
already valid. ``squad.names`` asks the team model to translate English display
names and writes ``name_zh`` back by ``provider_id``. It does not look up a
name source and does not call ``maintain_team_data``.
"""
from __future__ import annotations

import asyncio

from .skill_catalog import PANEL_LEAF_REASON_CODES, PANEL_LEAVES, PANEL_REASON_CODES

WORKFLOW = "team"
LEAVES = PANEL_LEAVES
REASON_CODES = PANEL_REASON_CODES

_PRIORITY = (
    "RATE_LIMITED", "SOURCE_UNAVAILABLE", "MODEL_FAILED", "INTERRUPTED", "INCOMPLETE_SCHEDULE",
    "STALE", "MISSING", "NAMESAKE_BLOCKED", "CREST_MISSING",
    "SAMPLE_INSUFFICIENT", "SCOPE_DENIED",
)
_BAD_ATTEMPT = {"failed", "unavailable", "interrupted"}
_CHILD_TOOLS = {
    "club.crest": ("get_team_data", "maintain_team_data", "read_evidence", "use_skill", "submit_answer"),
    "squad.info": ("get_team_data", "maintain_team_data", "read_evidence", "use_skill", "submit_answer"),
    "squad.names": ("get_team_data", "use_skill", "submit_answer"),
    "squad.images": ("get_team_data", "maintain_team_data", "read_evidence", "use_skill", "submit_answer",
                     "photo_queue", "plan_photo_batch", "fetch_player_photos"),
    "schedule.standings": ("get_team_data", "maintain_team_data", "read_evidence", "use_skill", "submit_answer"),
    "stats.charts": ("get_team_data", "use_skill", "submit_answer"),
}


def child_tools(leaf):
    """Tools a leaf may receive: the skill list, cut by the parent capability set."""
    from .tool_registry import DELEGATION_TOOLS, TEAM_WORK, provides
    names = []
    for name in _CHILD_TOOLS[leaf]:
        if name in DELEGATION_TOOLS:
            continue
        if provides(name, TEAM_WORK):
            names.append(name)
    return names


def chart_blocks(view):
    """W/D/L, goals and home/away from one league sample. No network and no writes.

    A hidden spoiler view is ``SCOPE_DENIED``. A season with no finished league
    score is ``SAMPLE_INSUFFICIENT``: the three blocks are null, not zeros.
    """
    payload = view if isinstance(view, dict) else {}
    stats = payload.get("stats") if isinstance(payload.get("stats"), dict) else {}
    if stats.get("hidden"):
        return _empty_chart("SCOPE_DENIED", sample=None)
    league = payload.get("league") if isinstance(payload.get("league"), dict) else {}
    season = payload.get("season") if isinstance(payload.get("season"), dict) else {}
    league_id = league.get("id")
    finished = []
    for match in payload.get("matches") or []:
        if not isinstance(match, dict):
            continue
        if league_id and match.get("competition_id") != league_id:
            continue
        if match.get("status") != "finished":
            continue
        if match.get("goals_for") is None or match.get("goals_against") is None:
            continue
        finished.append(match)
    scope = "所属联赛已完赛且比分完整的比赛；不含杯赛与欧战。"
    sources = _chart_sources(payload, finished)
    sample = {
        "played": len(finished),
        "from": None,
        "to": None,
        "season_id": season.get("id"),
        "competition": league.get("name") or league_id,
        "scope": scope,
        "sources": sources,
    }
    if not finished:
        return _empty_chart("SAMPLE_INSUFFICIENT", sample=sample)
    dates = sorted(match.get("kickoff_date") for match in finished if match.get("kickoff_date"))
    sample["from"] = dates[0] if dates else None
    sample["to"] = dates[-1] if dates else None
    home = [match for match in finished if match.get("venue") == "home"]
    away = [match for match in finished if match.get("venue") == "away"]
    season_tally = _tally(finished)
    return {
        "status": "ready",
        "reason_code": None,
        "retryable": False,
        "sample": sample,
        "results": {key: season_tally[key] for key in ("won", "drawn", "lost", "played")},
        "goals": {key: season_tally[key] for key in ("goals_for", "goals_against", "played")},
        "home_away": {"home": _venue_block(_tally(home)), "away": _venue_block(_tally(away))},
    }


def progress_from(leaves):
    """Map leaf status onto the completion bar.

    已完成 counts filled leaves, including a crest that only lacks a local asset
    and a photo leaf whose only gap is a namesake block. 待处理 is what is
    still running or still retryable. 未完成 is a failed or interrupted leaf.
    ``SAMPLE_INSUFFICIENT`` and ``SCOPE_DENIED`` are settled: neither done nor
    failed.
    """
    completed = failed = remaining = settled = 0
    for leaf in leaves or []:
        status = leaf.get("status")
        if status == "filled":
            completed += 1
        elif status in {"failed", "interrupted"}:
            failed += 1
        elif status == "running" or (status == "gap" and leaf.get("actionable")):
            remaining += 1
        else:
            settled += 1
    return {"completed": completed, "failed": failed, "remaining": remaining, "settled": settled}


def interrupt(leaves):
    """Stop dispatch. An in-flight leaf is interrupted and is not retryable."""
    output = []
    for leaf in leaves or []:
        current = dict(leaf)
        if current.get("status") == "running":
            current.update(status="interrupted", reason_code="INTERRUPTED", retryable=False,
                           actionable=False, summary="更新已中断，已完成的部分仍保留。")
        output.append(current)
    return output


def assess(store, team_id, *, reveal=True):
    """One gap snapshot. Stub views are tolerated; this function does not write."""
    from . import team_center
    try:
        view = team_center.center_view(store, team_id, reveal=reveal)
    except Exception as exc:
        view = {"roster": [], "components": {}, "errors": [str(exc)], "stats": {"hidden": False}}
    if not isinstance(view, dict):
        view = {}
    leaves = [
        _crest_leaf(view),
        _info_leaf(view),
        _names_leaf(view),
        _images_leaf(view),
        _schedule_leaf(view),
        _charts_leaf(view),
    ]
    count = _actionable_count(leaves)
    season = view.get("season") if isinstance(view.get("season"), dict) else {}
    return {
        "workflow": WORKFLOW,
        "team_id": team_id,
        "season_id": season.get("id"),
        "actionable_count": count,
        "early_stop": count == 0,
        "summary": ("没有仍可重试的缺项，已保留现有资料。" if count == 0 else "正在按缺项补全球队资料。"),
        "leaves": leaves,
        "skills": leaves,
        "spawn": [leaf["id"] for leaf in leaves if leaf.get("actionable")],
    }


def panel_payload(store, workflows, team_id):
    """Live gaps plus the workflow run the team page already polls."""
    assessed = assess(store, team_id, reveal=True)
    run = workflows.status(WORKFLOW, team_id)
    return {
        "workflow": WORKFLOW,
        "team_id": team_id,
        "season_id": assessed.get("season_id"),
        "actionable_count": assessed["actionable_count"],
        "early_stop": assessed["early_stop"],
        "skills": assessed["leaves"],
        "run": {
            "status": run.get("status"),
            "phase": run.get("phase"),
            "message": run.get("message"),
            "progress": run.get("progress"),
            "last_error": run.get("last_error"),
            "updated_at": run.get("updated_at"),
            "skills": run.get("leaves") or [],
            "panel": run.get("panel"),
        },
    }


async def run_panel(manager, entry, first):
    """Dispatch the actionable leaves once, then persist whatever succeeded."""
    from . import team_center
    store = manager.store
    assessed = assess(store, entry.team_id, reveal=True)
    if assessed["actionable_count"] == 0:
        _publish(manager, entry, assessed["leaves"], message=assessed["summary"],
                 summary=assessed["summary"], early_stop=True, spawned=[], phase="maintaining")
        return
    leaves = [dict(leaf) for leaf in assessed["leaves"]]
    spawned = [leaf["id"] for leaf in leaves if leaf.get("actionable")]
    schedule = _find(leaves, "schedule.standings")
    charts = _find(leaves, "stats.charts")
    if (schedule["id"] in spawned or schedule.get("status") == "filled") and charts.get("status") != "filled":
        if charts["id"] not in spawned:
            spawned.append(charts["id"])
    # Identity keys are not on the snapshot yet. This round's roster write is
    # what makes a translation possible, so names follow info instead of waiting
    # for another click. A roster that already has keys leaves names actionable
    # on its own, and it can run beside photos.
    if "squad.info" in spawned and "squad.names" not in spawned:
        spawned.append("squad.names")
    spawned.sort(key=lambda leaf_id: LEAVES.index(leaf_id))
    for leaf in leaves:
        if leaf["id"] in spawned:
            leaf["status"] = "running"
            leaf["spawned"] = True
            leaf["actionable"] = False
        if leaf["id"] == "stats.charts":
            leaf["depends_on"] = ["schedule.standings"]
            leaf["parallel"] = False
        elif leaf["id"] in {"squad.images", "squad.names"} and "squad.info" in spawned:
            leaf["depends_on"] = ["squad.info"]
    data_parts = []
    for leaf in leaves:
        if leaf["id"] not in spawned or leaf["id"] in {"stats.charts", "squad.images", "squad.names"}:
            continue
        for part in leaf.get("maintain_parts") or []:
            if part not in data_parts:
                data_parts.append(part)
    _publish(manager, entry, leaves, message="正在按缺项补全球队资料。",
             summary="正在按缺项补全球队资料。", early_stop=False, spawned=spawned, phase="maintaining")
    images_wanted = "squad.images" in spawned
    names_wanted = "squad.names" in spawned
    outcome = {"components": {}}
    photos = None
    names_result = None
    photos_ran = False
    names_ran = False
    try:
        async def maintain(context):
            async with manager.team_slots:
                return await team_center.maintain_team(
                    store, entry.team_id, force=first, **({"parts": data_parts} if data_parts else {}))

        async def portraits(context):
            return await asyncio.wait_for(
                team_center.sync_portraits(store, entry.team_id, force=first, budget_seconds=150), 180)

        async def translate(context):
            from .squad_names import translate_roster
            return await translate_roster(store, entry.team_id)

        async def run_maintain():
            try:
                return await manager._unit(entry, maintain, {"team_id": entry.team_id})
            except asyncio.CancelledError:
                raise
            except Exception:
                return {"components": {part: {"status": "failed", "errors": ["来源暂不可用"]} for part in data_parts},
                        "errors": ["来源暂不可用"]}

        async def run_photos():
            try:
                return await manager._unit(entry, portraits, {"team_id": entry.team_id, "part": "portraits"})
            except asyncio.CancelledError:
                raise
            except Exception:
                return {"status": "failed", "errors": ["球员照片来源暂时无法读取"]}

        async def run_names():
            try:
                return await manager._unit(entry, translate, {"team_id": entry.team_id, "part": "names"})
            except asyncio.CancelledError:
                raise
            except Exception:
                return {"status": "failed", "errors": ["译名模型暂不可用"]}

        async def side_work():
            jobs = []
            labels = []
            if names_wanted:
                jobs.append(asyncio.create_task(run_names()))
                labels.append("names")
            if images_wanted:
                jobs.append(asyncio.create_task(run_photos()))
                labels.append("photos")
            if not jobs:
                return None, None
            try:
                done = await asyncio.gather(*jobs)
            except asyncio.CancelledError:
                for job in jobs:
                    job.cancel()
                await asyncio.gather(*jobs, return_exceptions=True)
                raise
            translated = pictured = None
            for label, value in zip(labels, done):
                if label == "names":
                    translated = value
                else:
                    pictured = value
            return translated, pictured

        roster_rewritten = "roster" in data_parts
        if data_parts and not roster_rewritten and (names_wanted or images_wanted):
            maintain_task = asyncio.create_task(run_maintain())
            side_task = asyncio.create_task(side_work())
            try:
                outcome, side = await asyncio.gather(maintain_task, side_task)
            except asyncio.CancelledError:
                maintain_task.cancel()
                side_task.cancel()
                await asyncio.gather(maintain_task, side_task, return_exceptions=True)
                raise
            names_result, photos = side
            names_ran = names_wanted
            photos_ran = images_wanted
        elif data_parts:
            outcome = await run_maintain()
            if names_wanted or images_wanted:
                names_result, photos = await side_work()
                names_ran = names_wanted
                photos_ran = images_wanted
        elif names_wanted or images_wanted:
            names_result, photos = await side_work()
            names_ran = names_wanted
            photos_ran = images_wanted
        final = assess(store, entry.team_id, reveal=True)
        settled = final["leaves"]
        _apply_outcome(settled, outcome if isinstance(outcome, dict) else {}, data_parts,
                       photos if isinstance(photos, dict) else None, photos_ran,
                       names_result if isinstance(names_result, dict) else None, names_ran)
        _mark_spawned(settled, spawned)
        note = _photo_note(photos) if photos_ran else None
        notes = []
        if isinstance(outcome, dict) and outcome.get("errors"):
            notes.append("部分球队资料暂未更新，仍可查看上次有效资料。")
        if note:
            notes.append(note)
        progress = progress_from(settled)
        summary = _round_summary(progress, settled)
        _publish(manager, entry, settled, message=summary, summary=summary,
                 early_stop=_actionable_count(settled) == 0, spawned=spawned, phase="maintaining",
                 last_error="；".join(notes) or None)
    except asyncio.CancelledError:
        current = manager.status(entry.kind, entry.team_id)
        stopped = interrupt(current.get("leaves") or leaves)
        summary = "更新已中断，已完成的部分仍保留。"
        _publish(manager, entry, stopped, message=summary, summary=summary, early_stop=False,
                 spawned=spawned, phase="maintaining")
        raise


def _empty_chart(reason, *, sample):
    return {"status": "empty", "reason_code": reason, "retryable": REASON_CODES[reason],
            "sample": sample, "results": None, "goals": None, "home_away": None}


def _tally(matches):
    """W/D/L, goals, points and goal difference over one set of matches.

    ``team_center`` has its own ``_tally`` that already computes points and goal
    difference, and this one used to stop at the raw counts. The two are fed the
    same matches and are read by the same panel, so a field present in one and
    absent from the other is a field that disappears depending on which path
    built the payload — which is what put an em dash in the home/away row beside
    a record the source had already stated. Both derived values are arithmetic
    on the same goals rather than separate source claims, so computing them here
    asserts nothing new.
    """
    won = drawn = lost = goals_for = goals_against = 0
    for match in matches:
        scored_for = match["goals_for"]
        scored_against = match["goals_against"]
        goals_for += scored_for
        goals_against += scored_against
        if scored_for > scored_against:
            won += 1
        elif scored_for < scored_against:
            lost += 1
        else:
            drawn += 1
    return {"played": len(matches), "won": won, "drawn": drawn, "lost": lost,
            "goals_for": goals_for, "goals_against": goals_against,
            "points": won * 3 + drawn,
            "goal_difference": goals_for - goals_against}


def _venue_block(tally):
    """One venue's record, with the fields the page actually reads.

    ``points`` and ``goal_difference`` are in the whitelist because ``_tally``
    computes them and dropping them here is what made the home/away panel show
    an em dash beside a record that was known: the block is preferred over
    ``stats.home``/``stats.away`` when the chart blocks are ready, so a field
    missing from this dict is a field missing from the page. Both are arithmetic
    on the same tallies rather than separate source claims, so carrying them is
    not asserting anything the source did not already say.
    """
    return {key: tally[key] for key in ("won", "drawn", "lost", "goals_for", "goals_against",
                                        "played", "points", "goal_difference")}


def _chart_sources(view, matches):
    sources = []
    seen = set()
    for source in view.get("sources") or []:
        if not isinstance(source, dict) or not source.get("url") or source["url"] in seen:
            continue
        seen.add(source["url"])
        sources.append({"name": source.get("name"), "url": source["url"]})
    for match in matches:
        url = match.get("source_url")
        if url and url not in seen:
            seen.add(url)
            sources.append({"name": "比赛来源", "url": url})
    return sources


def _transport(errors):
    text = " ".join(str(item) for item in errors or [])
    lowered = text.lower()
    if "429" in lowered or "rate limit" in lowered or "too many" in lowered or "限流" in text:
        return "RATE_LIMITED"
    return "SOURCE_UNAVAILABLE"


def _gap(reason, *, detail, part=None, retryable=None):
    flag = REASON_CODES[reason] if retryable is None else bool(retryable)
    row = {"reason_code": reason, "retryable": flag, "detail": detail}
    if part:
        row["part"] = part
    return row


def _primary(gaps):
    if not gaps:
        return None, False
    chosen = sorted(gaps, key=lambda gap: _PRIORITY.index(gap["reason_code"]) if gap["reason_code"] in _PRIORITY else 99)[0]
    return chosen["reason_code"], bool(chosen["retryable"])


def _leaf(leaf_id, *, filled, gaps, summary, updated_at, maintain_parts, extra=None, depends_on=None):
    reason, retryable = _primary(gaps)
    actionable = any(gap.get("retryable") for gap in gaps)
    if filled and not actionable:
        status = "filled"
    elif actionable:
        status = "gap"
    else:
        status = "settled"
    row = {
        "id": leaf_id,
        "topic": leaf_id,
        "status": status,
        "actionable": actionable and status == "gap",
        "reason_code": reason,
        "retryable": retryable,
        "summary": summary,
        "gaps": gaps,
        "updated_at": updated_at,
        "tools": child_tools(leaf_id),
        "parallel": leaf_id != "stats.charts",
        "depends_on": ["schedule.standings"] if leaf_id == "stats.charts" else list(depends_on or []),
        "spawned": False,
        "maintain_parts": list(maintain_parts or []),
    }
    if extra:
        row.update(extra)
    return row


def _component(view, name):
    components = view.get("components") if isinstance(view.get("components"), dict) else {}
    component = components.get(name)
    return component if isinstance(component, dict) else {}


def _failed_gap(component, *, detail, part):
    if component.get("status") in _BAD_ATTEMPT:
        return _gap(_transport(component.get("errors")), detail=detail, part=part)
    return None


def _crest_leaf(view):
    profile = _component(view, "profile")
    if not profile:
        return _leaf("club.crest", filled=False, gaps=[_gap("MISSING", detail="还没有俱乐部资料。", part="profile")],
                     summary="俱乐部资料还没有快照。", updated_at=None, maintain_parts=["profile"])
    crest = profile.get("crest") if isinstance(profile.get("crest"), dict) else {}
    local = bool(crest.get("local_asset") and crest.get("status") == "fresh"
                 and str(crest.get("url") or "").startswith("/assets/"))
    gaps = []
    parts = []
    failed = _failed_gap(profile, detail="俱乐部资料来源暂不可用。", part="profile")
    if failed:
        gaps.append(failed)
        parts.append("profile")
    missing = profile.get("missing_fields") or []
    if missing and not failed:
        reason = "STALE" if profile.get("status") == "stale" or profile.get("stale") else "MISSING"
        gaps.append(_gap(reason, detail="缺少：" + "、".join(str(field) for field in missing), part="profile"))
        parts.append("profile")
    elif not missing and (profile.get("status") == "stale" or profile.get("stale")) and not failed:
        gaps.append(_gap("STALE", detail="俱乐部资料已过期。", part="profile"))
        parts.append("profile")
    if not local:
        gaps.append(_gap("CREST_MISSING", detail=crest.get("note") or "队徽缺少可用的本地资产。"))
    filled = not any(gap["retryable"] for gap in gaps) and (local or any(gap["reason_code"] == "CREST_MISSING" for gap in gaps))
    reason, _ = _primary([gap for gap in gaps if gap["reason_code"] == "CREST_MISSING"] or gaps)
    summary = "队徽是可用的本地资产。" if local and filled and reason != "CREST_MISSING" else (
        "队徽缺少本地资产，不再下载。" if filled else "俱乐部资料仍有可重试的缺口。")
    return _leaf("club.crest", filled=filled, gaps=gaps, summary=summary,
                 updated_at=profile.get("updated_at"), maintain_parts=parts)


def _identity(row):
    return str(row.get("provider_id") or row.get("id") or "").strip()


def _info_leaf(view):
    roster = [row for row in (view.get("roster") or []) if isinstance(row, dict)]
    component = _component(view, "roster")
    gaps = []
    parts = []
    failed = _failed_gap(component, detail="阵容来源暂不可用。", part="roster")
    if failed:
        gaps.append(failed)
        parts.append("roster")
    elif not roster:
        gaps.append(_gap("MISSING", detail="还没有阵容快照。", part="roster"))
        parts.append("roster")
    elif component.get("status") in {"stale", "missing"} or component.get("stale"):
        reason = "STALE" if component.get("status") == "stale" or component.get("stale") else "MISSING"
        gaps.append(_gap(reason, detail="阵容快照需要更新。", part="roster"))
        parts.append("roster")
    identity_gaps = [{"player_id": _identity(row), "name": row.get("name") or ""}
                     for row in roster if not _identity(row)]
    if identity_gaps and not failed:
        gaps.append(_gap("MISSING", detail="有球员缺少身份键。", part="roster"))
        if "roster" not in parts:
            parts.append("roster")
    name_gaps = []
    for row in roster:
        if not str(row.get("name") or "").strip():
            name_gaps.append({"player_id": _identity(row), "name": "", "field": "name"})
        elif not row.get("name_zh"):
            name_gaps.append({"player_id": _identity(row), "name": row.get("name") or "", "field": "name_zh"})
    filled = bool(roster) and not any(gap["retryable"] for gap in gaps)
    summary = "阵容快照有效。" if filled else "阵容仍有可重试的缺口。"
    return _leaf("squad.info", filled=filled, gaps=gaps, summary=summary,
                 updated_at=component.get("updated_at"), maintain_parts=parts,
                 extra={"name_gaps": name_gaps, "identity_gaps": identity_gaps})


def _names_leaf(view):
    """Chinese display names, translated by the team model and stored by id.

    A row is done when ``name_zh`` is non-empty. A suspicious reading is still
    done: this leaf does not judge the translation. A row with no English name
    is ``MISSING`` and is not retried. A recorded model failure is
    ``MODEL_FAILED`` and is retried. Anything else with an English name and no
    Chinese name keeps the leaf open. The leaf does not start before every
    roster row has a ``provider_id``.
    """
    roster = [row for row in (view.get("roster") or []) if isinstance(row, dict)]
    component = _component(view, "roster")
    flags = PANEL_LEAF_REASON_CODES["squad.names"]
    waiting = _leaf(
        "squad.names", filled=False, gaps=[],
        summary="尚无名单或身份键，中文名随阵容补齐后再译。",
        updated_at=component.get("updated_at"), maintain_parts=[],
        extra={"depends_on": ["squad.info"], "parallel": False,
               "named_count": 0, "roster_count": len(roster), "player_gaps": []})
    if not roster or any(not _identity(row) for row in roster):
        return waiting
    player_gaps = []
    pending = 0
    for row in roster:
        provider_id = _identity(row)
        english = str(row.get("name") or "").strip()
        chinese = str(row.get("name_zh") or "").strip()
        if chinese:
            continue
        code = str(row.get("name_zh_reason_code") or "")
        if not english:
            player_gaps.append({"provider_id": provider_id, "reason_code": "MISSING",
                                "retryable": flags["MISSING"], "detail": "没有英文名可译。"})
            continue
        if code == "MODEL_FAILED":
            player_gaps.append({"provider_id": provider_id, "reason_code": "MODEL_FAILED",
                                "retryable": flags["MODEL_FAILED"], "detail": "中文名翻译没有完成。"})
            continue
        if code == "INTERRUPTED":
            player_gaps.append({"provider_id": provider_id, "reason_code": "INTERRUPTED",
                                "retryable": flags["INTERRUPTED"], "detail": "翻译被中断。"})
            continue
        pending += 1
        player_gaps.append({"provider_id": provider_id, "reason_code": None,
                            "retryable": True, "detail": "待从英文显示名译成大陆中文名。"})
    gaps = []
    if pending:
        gaps.append(_gap("MISSING", detail=f"还有 {pending} 名球员待译成中文名。"))
    for player in player_gaps:
        if player["reason_code"] in flags:
            gaps.append(_gap(player["reason_code"], detail=player["detail"], retryable=player["retryable"]))
    named = len(roster) - len(player_gaps)
    filled = not any(gap["retryable"] for gap in gaps)
    summary = "每名球员都有中文名，或只剩不可重试的缺口。" if filled else "还有球员的中文名待译。"
    return _leaf("squad.names", filled=filled, gaps=gaps, summary=summary,
                 updated_at=component.get("updated_at"), maintain_parts=[],
                 extra={"depends_on": [], "parallel": True, "named_count": named,
                        "roster_count": len(roster), "player_gaps": player_gaps})


def _images_leaf(view):
    from .photo_acceptance import assess as photo_assess
    roster = [row for row in (view.get("roster") or []) if isinstance(row, dict)]
    component = _component(view, "portraits")
    if not roster:
        return _leaf("squad.images", filled=False, gaps=[], summary="尚无名单，照片随名单补齐后再核。",
                     updated_at=component.get("updated_at"), maintain_parts=[],
                     extra={"photo_count": 0, "roster_count": 0, "actionable_count": 0})
    report = photo_assess(roster)
    route = list(report.get("retryable") or [])
    unattempted = int((report.get("counts") or {}).get("fix_unknown") or 0)
    namesakes = list(report.get("namesake_blocked") or [])
    gaps = []
    actionable = bool(route) or unattempted > 0
    if component.get("status") in _BAD_ATTEMPT and actionable:
        gaps.append(_gap(_transport(component.get("errors") or [row.get("reason_code") for row in route]),
                         detail="照片来源暂不可用。"))
    elif route or unattempted:
        codes = [str(row.get("reason_code") or "") for row in route]
        if any(code == "http_rate_limited" or "rate" in code for code in codes):
            reason = "RATE_LIMITED"
        elif codes and all(code == "not_yet_resolved" for code in codes) and unattempted == 0:
            reason = "MISSING"
        elif component.get("status") in _BAD_ATTEMPT or any(code not in {"", "not_yet_resolved"} for code in codes):
            reason = "SOURCE_UNAVAILABLE" if any(code != "not_yet_resolved" for code in codes) else "MISSING"
        else:
            reason = "MISSING"
        gaps.append(_gap(reason, detail="仍有可重试的缺图。"))
    if namesakes and not actionable:
        gaps.append(_gap("NAMESAKE_BLOCKED", detail="同名遮挡，不再重试同一来源。"))
    filled = not actionable
    summary = "没有仍可重试的缺图。" if filled else "照片仍有可重试的缺口。"
    return _leaf("squad.images", filled=filled, gaps=gaps, summary=summary,
                 updated_at=component.get("updated_at"), maintain_parts=[],
                 extra={"photo_count": int(report.get("with_photo") or 0),
                        "roster_count": len(roster),
                        "actionable_count": int(report.get("actionable_count") or 0) + unattempted})


def _schedule_leaf(view):
    coverage = view.get("coverage") if isinstance(view.get("coverage"), dict) else {}
    matches = int(coverage.get("matches") or 0)
    expected = coverage.get("expected_matches")
    has_full = bool(coverage.get("has_full_schedule"))
    schedule = _component(view, "schedule")
    standings = _component(view, "standings")
    rows = [row for row in (view.get("standings") or []) if isinstance(row, dict)]
    league = view.get("league") if isinstance(view.get("league"), dict) else {}
    expected_teams = league.get("expected_team_count")
    table_complete = bool(expected_teams) and len(rows) >= expected_teams
    gaps = []
    parts = []
    schedule_gap = _coverage_gap(schedule, complete=has_full, have=matches,
                                 incomplete="INCOMPLETE_SCHEDULE", part="schedule",
                                 empty_detail="赛程还没有快照。", incomplete_detail="赛程尚未覆盖应有的主客场。",
                                 stale_detail="赛程快照已过期。")
    if schedule_gap:
        gaps.append(schedule_gap)
        parts.append("schedule")
    standings_gap = _coverage_gap(standings, complete=table_complete, have=len(rows),
                                  incomplete="MISSING", part="standings",
                                  empty_detail="积分榜还没有快照。", incomplete_detail="积分榜还不是完整排名。",
                                  stale_detail="积分榜快照已过期。")
    if standings_gap:
        gaps.append(standings_gap)
        parts.append("standings")
    filled = not any(gap["retryable"] for gap in gaps) and has_full and table_complete
    # A non-retryable terminal other than an incomplete schedule still counts
    # as filled for the part that cannot be retried. Both parts must be there,
    # or each remaining gap must be non-retryable.
    if not filled and gaps and not any(gap["retryable"] for gap in gaps) and not any(
            gap["reason_code"] == "INCOMPLETE_SCHEDULE" for gap in gaps):
        filled = True
    summary = "赛程和积分榜都已齐。" if filled else "赛程或积分榜仍有缺口。"
    updated = schedule.get("updated_at") or standings.get("updated_at")
    return _leaf("schedule.standings", filled=filled, gaps=gaps, summary=summary, updated_at=updated,
                 maintain_parts=parts,
                 extra={"schedule": {"matches": matches, "expected_matches": expected,
                                     "has_full_schedule": has_full},
                        "standings": {"rows": len(rows), "expected_teams": expected_teams,
                                      "complete": table_complete}})


def _coverage_gap(component, *, complete, have, incomplete, part, empty_detail, incomplete_detail, stale_detail):
    failed = _failed_gap(component, detail="来源暂不可用。", part=part)
    if failed:
        return failed
    stale = component.get("status") == "stale" or bool(component.get("stale"))
    if complete and not stale and component.get("status") not in {None, "missing", "failed"}:
        return None
    if complete and stale:
        return _gap("STALE", detail=stale_detail, part=part)
    if not complete and have:
        return _gap(incomplete, detail=incomplete_detail, part=part)
    if not complete:
        return _gap("STALE" if stale else "MISSING", detail=empty_detail, part=part)
    return None


def _charts_leaf(view):
    blocks = chart_blocks(view)
    sample = blocks.get("sample") or None
    public_sample = None
    if isinstance(sample, dict):
        public_sample = {key: sample.get(key) for key in ("played", "from", "to", "season_id", "competition", "scope")}
    reason = blocks.get("reason_code")
    gaps = [_gap(reason, detail="图表没有可用的已完赛样本。" if reason == "SAMPLE_INSUFFICIENT" else "避剧透下不展示图表数字。")] if reason else []
    filled = blocks.get("status") == "ready"
    summary = ("图表数字可以追溯到赛程样本。" if filled
               else "图表样本不足，未编造胜平负或进失球。" if reason == "SAMPLE_INSUFFICIENT"
               else "图表因避剧透未展示数字。" if reason == "SCOPE_DENIED"
               else "图表尚未计算。")
    return _leaf("stats.charts", filled=filled, gaps=gaps, summary=summary, updated_at=None, maintain_parts=[],
                 extra={"sample": public_sample})


def _find(leaves, leaf_id):
    return next(leaf for leaf in leaves if leaf["id"] == leaf_id)


def _actionable_count(leaves):
    total = 0
    for leaf in leaves:
        if leaf.get("status") in {"filled", "interrupted", "running"}:
            continue
        if leaf.get("status") == "settled" and not leaf.get("retryable"):
            continue
        if leaf.get("retryable") or leaf.get("actionable"):
            total += 1
    return total


def _mark_spawned(leaves, spawned):
    info_spawned = "squad.info" in spawned
    for leaf in leaves:
        leaf["spawned"] = leaf["id"] in spawned
        leaf["parallel"] = leaf["id"] != "stats.charts"
        if leaf["id"] == "stats.charts":
            leaf["depends_on"] = ["schedule.standings"]
        elif leaf["id"] in {"squad.images", "squad.names"} and info_spawned:
            leaf["depends_on"] = ["squad.info"]


def _apply_outcome(leaves, outcome, attempted, photos, photos_ran, names_result=None, names_ran=False):
    components = outcome.get("components") if isinstance(outcome.get("components"), dict) else {}
    by_id = {leaf["id"]: leaf for leaf in leaves}

    def bad(part):
        status = (components.get(part) or {}).get("status")
        return part in attempted and status in _BAD_ATTEMPT

    if bad("profile"):
        _override(by_id["club.crest"], components.get("profile") or {})
    if bad("roster"):
        _override(by_id["squad.info"], components.get("roster") or {})
    failed_parts = [components.get(part) or {} for part in ("schedule", "standings") if bad(part)]
    if failed_parts:
        errors = [error for part in failed_parts for error in (part.get("errors") or [])]
        status = "interrupted" if any(part.get("status") == "interrupted" for part in failed_parts) else "failed"
        _override(by_id["schedule.standings"], {"status": status, "errors": errors})
    if photos_ran and isinstance(photos, dict) and photos.get("status") in _BAD_ATTEMPT:
        if by_id["squad.images"].get("status") != "filled":
            _override(by_id["squad.images"], photos)
    if names_ran and isinstance(names_result, dict) and names_result.get("status") in _BAD_ATTEMPT:
        leaf = by_id["squad.names"]
        if leaf.get("status") != "filled":
            if names_result.get("status") == "interrupted":
                _override(leaf, names_result)
            else:
                leaf.update(status="gap", reason_code="MODEL_FAILED", retryable=True, actionable=True,
                            summary="译名模型暂不可用。")


def _override(leaf, component):
    # Keep the snapshot time already on the leaf. A failed attempt must not
    # look like a fresh write.
    if component.get("status") == "interrupted":
        leaf.update(status="interrupted", reason_code="INTERRUPTED", retryable=False, actionable=False,
                    summary="更新已中断，已完成的部分仍保留。")
        return
    reason = _transport(component.get("errors"))
    leaf.update(status="failed", reason_code=reason, retryable=REASON_CODES[reason], actionable=False,
                summary="这一片暂未更新，已保留上次有效资料。")


def _photo_note(photos):
    if not isinstance(photos, dict) or photos.get("status") == "completed":
        return None
    if photos.get("errors"):
        return "球员照片来源暂时无法读取，已保留核心资料和通过核验的照片。"
    if photos.get("remaining_count"):
        return f"已核对本批照片，还有 {photos['remaining_count']} 名球员待核对；再次启动会接续补图。"
    return "部分球员暂无通过身份与许可核验的照片，已保留缺图原因。"


def _round_summary(progress, leaves):
    text = f"本轮资料：已完成 {progress['completed']}，待处理 {progress['remaining']}，未完成 {progress['failed']}。"
    charts = _find(leaves, "stats.charts")
    if charts.get("reason_code") == "SAMPLE_INSUFFICIENT":
        text += "图表样本不足，未编造胜平负或进失球。"
    elif charts.get("reason_code") == "SCOPE_DENIED":
        text += "图表因避剧透未展示数字。"
    return text


def _publish(manager, entry, leaves, *, message, summary, early_stop, spawned, phase, last_error=None):
    public = [_public_leaf(leaf) for leaf in leaves]
    progress = progress_from(public)
    manager.update(entry, phase=phase, message=message, progress=progress, leaves=public,
                   actionable_count=_actionable_count(public),
                   panel={"workflow": WORKFLOW, "summary": summary, "early_stop": early_stop,
                          "spawned": list(spawned), "order": list(LEAVES),
                          "serial": ["stats.charts"]},
                   last_error=last_error)


def _public_leaf(leaf):
    hidden = {"results", "goals", "home_away", "won", "lost", "drawn", "goals_for", "goals_against"}
    return {key: value for key, value in leaf.items() if key not in hidden}
