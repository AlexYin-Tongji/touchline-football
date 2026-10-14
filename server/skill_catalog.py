"""Discoverable skills, with compatible task-specific workflow routing.

A skill is a tree, not a file. The top of each tree is the perspective the task
was started from; below it sit the directions a question can take, and the
caller loads the one it needs. Loading is what keeps the tool surface honest —
a task that declared the directions it needs does not have to carry the
instructions for the ones it does not.

The tree is data, not prose, so a direction can be asserted about: ``TOPICS``
is the only place a topic's file is named, and ``load_topic`` is the only way
one is read. A path is never built from model input.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
import re


SKILL_ROOT = Path(__file__).resolve().parents[1] / "agent_skills"
PRIMARY_SKILLS = {
    "team-maintenance": ("球队维护", "team_refresh"),
    # Panel 「更新/补全」 only. A separate tree from team-maintenance: the parent
    # loads this skill and dispatches the five leaves below. It does not replace
    # team_refresh, which other entry points still run.
    "team": ("球队中心更新", "team"),
    "team-news": ("球队动态", "team_brief"),
    "team-information": ("球队信息", "team_maintenance"),
    "football-assistant": ("问答助手", "ask"),
}
WORKFLOWS = {
    "news_report": ("team-news", "news-writing.md"),
    "team_brief": ("team-news", "briefing.md"),
    "team_maintenance": ("team-information", "data-scope.md"),
    "team_refresh": ("team-maintenance", None),
    "team": ("team", None),
    "ask": ("football-assistant", None),
    "match_preview": ("football-assistant", "preview.md"),
    "match_review": ("football-assistant", "review.md"),
    "event_watch": ("football-assistant", "event.md"),
    "transfer_check": ("football-assistant", "transfer.md"),
    "tactics_explainer": ("football-assistant", "tactics.md"),
}

# The directions each skill's tree offers, and the file behind each. A topic id
# is what a caller asks for; the path is what gets read. Two levels are enough
# for the questions this product answers — a direction and its branches — so the
# id is dotted ("squad.images") rather than a chain of calls.
#
# `tool_capabilities` narrows what a topic may use. It is the topic's own
# declaration of the work it does, and the tool surface is built from it, so a
# topic that only talks about photos cannot call the article reader. A topic
# without one inherits its skill's set.
TOPICS = {
    # Panel orchestration leaves, in PANEL_LEAVES order. One topic per child:
    # the parent loads only the top-level skill and the child loads its leaf.
    "team": {
        "club.crest": {"path": "references/club-crest.md",
                       "description": "俱乐部资料与队徽状态。队徽只核对本地资产，不下载。"},
        "squad.info": {"path": "references/squad-info.md",
                       "description": "阵容，以及缺口需要时的球员姓名。"},
        "squad.images": {"path": "references/squad-images.md",
                         "description": "球员照片：缺口、同名遮挡、批次与验收。"},
        "schedule.standings": {"path": "references/schedule-standings.md",
                               "description": "赛程与所属联赛积分榜。"},
        "stats.charts": {"path": "references/stats-charts.md",
                         "description": "用赛程样本计算图表；样本不足则空态，不编造。"},
    },
    # The blocks a maintenance run is made of. One topic per block, because a
    # block is what gets handed to a sub-agent: the tree says how many there
    # are, and the run's structure follows the tree rather than a list in code.
    "team-maintenance": {
        "roster": {"path": "references/roster.md",
                   "description": "阵容：球员名单、号码、位置、出生日期与归属。"},
        "schedule": {"path": "references/schedule.md",
                     "description": "赛程：联赛与有参赛证据的欧战，完整度口径与避剧透。"},
        "standings": {"path": "references/standings.md",
                      "description": "积分榜：所属联赛完整排名与扣分口径。"},
        "portraits": {"path": "references/portraits.md",
                      "description": "球员照片：缺口归因、重试方向、批次与验收。"},
        "profile": {"path": "references/profile.md",
                    "description": "球队自身资料：创立年份、主场、昵称与队徽状态。"},
        "names": {"path": "references/names.md",
                  "description": "球员中文名：译名口径、身份核验与缺口。"},
    },
    "team-information": {
        "squad": {"path": "references/squad.md",
                  "description": "球员方向：分信息与图像两条，按问题选一条。"},
        "squad.info": {"path": "references/squad/info.md",
                       "description": "球员事实字段：国籍、出生日期、位置、号码、统计。"},
        "squad.images": {"path": "references/squad/images.md",
                         "description": "球员照片：缺口、来源、许可、重试与验收。"},
        "squad.review": {"path": "references/squad/review.md",
                         "description": "资料与照片复核：模型在代码已归类的候选集上决定重试谁、怎么重试、哪些需进一步核查。"},
        "team": {"path": "references/team.md",
                 "description": "俱乐部背景、赛程、积分榜与避剧透边界。"},
        "club": {"path": "references/club.md",
                 "description": "球队自身资料：创立年份、主场、昵称与队徽状态。"},
        "data": {"path": "references/data.md",
                 "description": "胜负、进失球、近期表现与样本范围。"},
        "scope": {"path": "references/data-scope.md",
                  "description": "数据范围与维护：快照状态、部分维护、统计口径。"},
    },
    "team-news": {
        "directions": {"path": "references/directions.md",
                       "description": "两个方向的差别与共同边界。"},
        "briefing": {"path": "references/briefing.md",
                     "description": "近况简报：一段时间内的多个事件。"},
        "news-writing": {"path": "references/news-writing.md",
                         "description": "新闻写作：围绕一个事件写连贯正稿。"},
    },
}

# Fixed spawn order for the panel workflow. Charts stay last: they read schedule
# samples (or a still-valid cached schedule) and do not fetch their own.
PANEL_LEAVES = (
    "club.crest",
    "squad.info",
    "squad.images",
    "schedule.standings",
    "stats.charts",
)
if tuple(TOPICS["team"]) != PANEL_LEAVES:
    raise RuntimeError("panel leaves drifted from TOPICS['team']")

# Shared gap reasons. The bool is whether this round may spawn that leaf again
# for the same reason. A later snapshot is a new decision, not a retry of a
# terminal code. CREST_MISSING is terminal because a crest is a shipped local
# asset, not a download. SAMPLE_INSUFFICIENT is the charts empty state.
PANEL_REASON_CODES = {
    "MISSING": True,
    "STALE": True,
    "SOURCE_UNAVAILABLE": True,
    "RATE_LIMITED": True,
    "NAMESAKE_BLOCKED": False,
    "CREST_MISSING": False,
    "INCOMPLETE_SCHEDULE": True,
    "SAMPLE_INSUFFICIENT": False,
    "SCOPE_DENIED": False,
    "INTERRUPTED": False,
}


def topics(skill):
    """The directions a skill's tree offers, for the catalogue and the prompt."""
    return {name: dict(value) for name, value in (TOPICS.get(skill) or {}).items()}


def topic_hint():
    """The topic ids a caller may load, written out for the tool description.

    This used to be a sentence typed by hand into the ``use_skill`` schema, and
    it drifted the moment a direction was added: the registry gained a topic and
    the description the model actually reads did not, so a block could exist,
    have a file, and still never be asked for by name. Generating it here keeps
    ``TOPICS`` the single place a direction is declared.
    """
    parts = []
    for skill, entries in TOPICS.items():
        ids = "、".join(entries)
        if ids:
            parts.append(f"{skill} 有 {ids}")
    return "；".join(parts)


def resolve_topic(skill, topic):
    """The path behind a topic id, refusing anything not in this skill's tree.

    The stored path is relative to the skill's own folder, because that is where
    a direction lives. ``_document`` reads from the skills root, so the folder is
    joined here — from the registry's own key, never from the caller's text.
    """
    if skill not in PRIMARY_SKILLS:
        raise ValueError("未知技能。")
    entry = (TOPICS.get(skill) or {}).get(str(topic))
    if entry is None:
        raise ValueError("未知工作方向。")
    return f"{skill}/{entry['path']}"


def resolve_workflow(skill, workflow=None):
    selected = PRIMARY_SKILLS[skill][1] if skill in PRIMARY_SKILLS else skill
    if selected not in WORKFLOWS:
        raise ValueError("未知技能或工作流程。")
    if workflow is not None:
        if workflow not in WORKFLOWS or WORKFLOWS[workflow][0] != WORKFLOWS[selected][0]:
            raise ValueError("该工作流程不属于所选技能。")
        selected = workflow
    return selected


@lru_cache(maxsize=64)
def _document(relative):
    # Paths come only from this registry. No model-provided path is interpolated.
    text = (SKILL_ROOT / relative).read_text(encoding="utf-8")
    if relative.endswith("/SKILL.md"):
        parts = text.split("---", 2)
        if len(parts) != 3 or parts[0].strip():
            raise ValueError("技能缺少规范元信息。")
        fields = {}
        for line in parts[1].splitlines():
            key, sep, value = line.partition(":")
            if sep:
                fields[key.strip()] = value.strip().strip('"\'')
        expected = relative.split("/")[0]
        if fields.get("name") != expected or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", expected):
            raise ValueError("技能名称与目录不一致。")
        if not fields.get("description") or len(fields["description"]) > 1024:
            raise ValueError("技能说明缺失或过长。")
        return fields, parts[2].strip()
    return {}, text.strip()


def skill_metadata():
    return [{"id": name, "name": title, "description": _document(name + "/SKILL.md")[0]["description"],
             "topics": [{"id": topic, "description": entry["description"]} for topic, entry in topics(name).items()]}
            for name, (title, _) in PRIMARY_SKILLS.items()]


def load_workflow(skill, workflow=None):
    selected = resolve_workflow(skill, workflow)
    folder, reference = WORKFLOWS[selected]
    sections = [_document(folder + "/SKILL.md")[1]]
    if reference:
        sections.append(_document(folder + "/references/" + reference)[1])
    # The shared protocol is written for report production: it fixes a
    # submission shape, a fact count policy and a revision path that only a
    # news draft has. Loading it into a question made ordinary answers read
    # like a filled-in form, so it applies to the news and team-information
    # skills only. A question keeps the SKILL.md boundaries, which already
    # carry the read-before-cite rule.
    if folder != "football-assistant":
        sections.append(_document("reader_protocol.md")[1])
    return "\n\n".join(sections)


def load_topic(skill, topic):
    """One direction's instructions, on their own.

    The skill's own file is not repeated: the caller already holds it, and a
    direction is a branch of it rather than a second procedure. What is
    returned is only the part that applies to the question being answered.
    """
    path = resolve_topic(skill, topic)
    return {"topic": str(topic), "skill": str(skill), "instructions": _document(path)[1]}
