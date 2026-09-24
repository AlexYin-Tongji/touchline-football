"""Three discoverable skills, with compatible task-specific workflow routing."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
import re


SKILL_ROOT = Path(__file__).resolve().parents[1] / "agent_skills"
PRIMARY_SKILLS = {
    "team-news": ("球队动态", "team_brief"),
    "team-information": ("球队信息", "team_maintenance"),
    "football-assistant": ("问答助手", "ask"),
}
WORKFLOWS = {
    "news_report": ("team-news", "news-writing.md"),
    "team_brief": ("team-news", "briefing.md"),
    "team_maintenance": ("team-information", "data-scope.md"),
    "ask": ("football-assistant", None),
    "match_preview": ("football-assistant", "preview.md"),
    "match_review": ("football-assistant", "review.md"),
    "event_watch": ("football-assistant", "event.md"),
    "transfer_check": ("football-assistant", "transfer.md"),
    "tactics_explainer": ("football-assistant", "tactics.md"),
}


def resolve_workflow(skill, workflow=None):
    selected = PRIMARY_SKILLS[skill][1] if skill in PRIMARY_SKILLS else skill
    if selected not in WORKFLOWS:
        raise ValueError("未知技能或工作流程。")
    if workflow is not None:
        if workflow not in WORKFLOWS or WORKFLOWS[workflow][0] != WORKFLOWS[selected][0]:
            raise ValueError("该工作流程不属于所选技能。")
        selected = workflow
    return selected


@lru_cache(maxsize=32)
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
    return [{"id": name, "name": title, "description": _document(name + "/SKILL.md")[0]["description"]}
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
