"""Which tools this task may use, decided by what the task needs.

Twenty tools were advertised to every task. The model then had to infer from
prose which of them applied, and the call records show what that costs: tools
that cannot work in the current scope were still offered and still called. Every
`read_article` call in the stored runs failed, because a task with a fixed match
scope refuses arbitrary discovery — the tool was simply not usable there and
nothing said so.

So a task declares capabilities, not tool names. A capability says what the task
needs to do ("read the original text", "look up a person"), and the registry
returns the tools that provide it, minus whatever the run's policy forbids. A
skill file can therefore stay a description of how to work rather than a list
that drifts out of date the moment a tool is renamed, and a task cannot widen
its own reach by naming a tool: policy is applied after the declaration, never
before it.

Tags are deliberately coarse. A capability per tool would be a rename of the
tool list; a capability per kind of work is something a skill can honestly claim.
"""
from __future__ import annotations

import copy
from functools import lru_cache

# What a tool is for. A tool may serve more than one kind of work; reading
# evidence serves both a news task and a question.
IDENTITY = "identity"      # who this club is, what it is following
EVIDENCE = "evidence"      # find and read text we can cite
NEWS = "news"              # discover and collect new material
TEAM = "team"              # squad, fixtures, standings, derived figures
PERSON = "person"          # one player's record and portrait
EVENTS = "events"          # what changed about a tracked event
MATCH = "match"            # one fixture's context and boundaries
PROFILE = "profile"        # the reader's own stated preferences
SKILL = "skill"            # load a working procedure
DELEGATE = "delegate"      # hand work to a sub-agent
OUTPUT = "output"          # deliver the answer

# Capability sets a skill may declare. Every shipped skill works from one of
# these; a skill naming something else is a configuration error, not a silent
# grant of nothing.
NEWS_WORK = frozenset({IDENTITY, EVIDENCE, NEWS, TEAM, PERSON, SKILL, DELEGATE, OUTPUT})
TEAM_WORK = frozenset({IDENTITY, EVIDENCE, TEAM, PERSON, SKILL, DELEGATE, OUTPUT})
ASK_WORK = frozenset({IDENTITY, EVIDENCE, NEWS, TEAM, PERSON, EVENTS, MATCH, PROFILE, SKILL, DELEGATE, OUTPUT})
MATCH_WORK = frozenset({IDENTITY, EVIDENCE, MATCH, TEAM, PERSON, SKILL, DELEGATE, OUTPUT})
EVENT_WORK = frozenset({IDENTITY, EVIDENCE, EVENTS, NEWS, SKILL, DELEGATE, OUTPUT})

# Which capability set each workflow works from. This is the one place that
# decides, so a workflow cannot accidentally inherit a wider set.
WORKFLOW_CAPABILITIES = {
    "news_report": NEWS_WORK,
    "team_brief": NEWS_WORK,
    "team_maintenance": TEAM_WORK,
    "ask": ASK_WORK,
    "match_preview": MATCH_WORK,
    "match_review": MATCH_WORK,
    "event_watch": EVENT_WORK,
    "transfer_check": ASK_WORK,
    "tactics_explainer": ASK_WORK,
}

# Tool name -> the kinds of work it does. Kept beside the registry rather than
# inside each schema so a tool's purpose is readable in one place.
TOOL_CAPABILITIES = {
    # The pack serves orientation, not everything it happens to contain. It
    # returns the reader's preferences too, but a task declares PROFILE only
    # when those preferences should affect its output — an automated news
    # report follows editorial judgement, not the reader's stated interests.
    "context_pack": (IDENTITY, TEAM, NEWS, EVENTS),
    "search_evidence": (EVIDENCE,),
    "search_team_knowledge": (EVIDENCE, TEAM),
    "search_passages": (EVIDENCE,),
    "compare_sources": (EVIDENCE,),
    "read_evidence": (EVIDENCE,),
    "read_article": (EVIDENCE, NEWS),
    "discover_news": (NEWS,),
    "refresh_team_sources": (NEWS,),
    "research_news": (NEWS, EVIDENCE),
    "event_timeline": (EVENTS,),
    "match_context": (MATCH, TEAM),
    "get_team_data": (TEAM, PERSON),
    "query_structured_sources": (TEAM,),
    "maintain_team_data": (TEAM, PERSON, NEWS),
    "fetch_player_photos": (PERSON,),
    "conversation_history": (IDENTITY,),
    "use_skill": (SKILL,),
    "submit_answer": (OUTPUT,),
    "revise_news": (OUTPUT,),
}

# The sub-agent tools are part of the delegation capability and are registered
# from their own module, so they are not repeated here.
DELEGATION_TOOLS = ("spawn_agent", "list_agents", "wait_agents", "send_agent",
                    "cancel_agent", "release_agent")


class UnknownCapability(ValueError):
    """A skill asked for work the registry does not know how to provision."""


def known(capability):
    return str(capability) in {
        IDENTITY, EVIDENCE, NEWS, TEAM, PERSON, EVENTS, MATCH, PROFILE, SKILL, DELEGATE, OUTPUT}


def declared(workflow):
    """The capability set a workflow works from."""
    value = WORKFLOW_CAPABILITIES.get(str(workflow))
    if value is None:
        raise UnknownCapability(f"未知工作流程：{workflow}。")
    return value


def provides(tool_name, capabilities):
    """Whether a tool serves any of the capabilities a task declared."""
    return bool(set(TOOL_CAPABILITIES.get(str(tool_name), ())) & set(capabilities))


def select(definitions, capabilities, *, allowed=None, excluded=()):
    """The tools a task may see.

    ``allowed`` is the sub-task allowlist and is applied last, so a child can
    never receive a tool its parent did not hold. ``excluded`` comes from the
    run's policy (a fixed match scope, no network, a demo mode) and is applied
    the same way the previous implementation applied it — this function decides
    relevance, not permission.
    """
    capabilities = set(capabilities)
    excluded = set(excluded)
    chosen = []
    for definition in definitions:
        name = definition.get("name") or (definition.get("function") or {}).get("name")
        if not name or name in excluded:
            continue
        if allowed is not None and name not in allowed:
            continue
        if name in DELEGATION_TOOLS:
            # Delegation tools travel with the capability, not with a tag on
            # each one: a sub-task gets all of them or none.
            if DELEGATE in capabilities:
                chosen.append(definition)
            continue
        if provides(name, capabilities):
            chosen.append(definition)
    return chosen


def summary(definitions):
    """A short description of a tool surface, for the trace and the settings page."""
    names = [item.get("name") or (item.get("function") or {}).get("name") for item in definitions]
    return {"count": len(names), "tools": [name for name in names if name]}
