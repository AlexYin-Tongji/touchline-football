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
    # A maintenance run is a tree of blocks, so its default set is TEAM_WORK and
    # each block narrows from it below. It is TEAM_WORK rather than a new set
    # because the work is the same work read from the other end: the blocks
    # refresh what the team-information skill reads.
    "team_refresh": TEAM_WORK,
    # Panel 「更新/补全」. Same work as a maintenance pass, read as six leaves
    # instead of the six team_refresh blocks. Not an alias of team_refresh.
    "team": TEAM_WORK,
    "ask": ASK_WORK,
    "match_preview": MATCH_WORK,
    "match_review": MATCH_WORK,
    "event_watch": EVENT_WORK,
    "transfer_check": ASK_WORK,
    "tactics_explainer": ASK_WORK,
}

# What a loaded direction narrows to. A topic is a branch of a skill, so it may
# only ever subtract from that skill's set — never add. The image direction is
# the clearest case: its whole job is the photo queue and a batch of lookups, so
# it carries no evidence reader and no news discovery, and those tools are not
# offered while it is the loaded direction.
#
# ``exposes`` is the one thing a topic may *add back*, and it is not a widening
# of the capability set: it names tools the skill already holds by capability but
# that are withheld from conversations by default. ``fetch_player_photos`` is the
# only such tool. It is background maintenance — thirty to ninety seconds of
# network work — so it is kept out of an ordinary conversation's surface; loading
# the image direction is the task saying that is exactly what it came to do. The
# run's own scope still decides permission afterwards, and nothing here can
# override it.
#
# A topic with no entry keeps its skill's set and exposes nothing extra.
TOPIC_CAPABILITIES = {
    "team-information:squad.images": {
        "capabilities": frozenset({IDENTITY, TEAM, PERSON, SKILL, OUTPUT}),
        "exposes": ("fetch_player_photos", "review_maintenance"),
    },
    # The review direction reads no article: its work is the local queues, the
    # club record and one paid judgement round over the residue code could not
    # settle. Narrower than the image direction, which also runs the fetch.
    "team-information:squad.review": {
        "capabilities": frozenset({IDENTITY, TEAM, PERSON, SKILL, OUTPUT}),
        "exposes": ("review_maintenance",),
    },
    "team-information:squad.info": {
        "capabilities": frozenset({IDENTITY, EVIDENCE, TEAM, PERSON, SKILL, DELEGATE, OUTPUT})},
    "team-information:team": {
        "capabilities": frozenset({IDENTITY, EVIDENCE, TEAM, SKILL, DELEGATE, OUTPUT})},
    "team-information:data": {
        "capabilities": frozenset({IDENTITY, EVIDENCE, TEAM, PERSON, SKILL, DELEGATE, OUTPUT})},
    "team-information:scope": {"capabilities": TEAM_WORK},
    "team-information:squad": {"capabilities": TEAM_WORK},

    # --- team-maintenance: one entry per block ---------------------------------
    # These are the blocks a maintenance run is split into. Unlike an ordinary
    # conversation's direction, a block *is* the work, so it has to keep the
    # tools that do it rather than narrow to a description of it.
    #
    # Every block keeps EVIDENCE on purpose. ``spawn_agent`` refuses a child
    # whose tool set includes a composite reader (maintain_team_data,
    # fetch_player_photos) without read_evidence, because those tools read
    # original text and the child would have no way to cite it. Narrowing
    # EVIDENCE away would make a block unable to hand its own tool to a child.
    #
    # ``fetch_player_photos`` and ``maintain_team_data`` are background tools,
    # withheld from ordinary conversations and restored only by ``exposes`` —
    # that is the mechanism that lets a block actually run its own batch.
    "team-maintenance:roster": {
        "capabilities": frozenset({IDENTITY, EVIDENCE, TEAM, PERSON, SKILL, DELEGATE, OUTPUT}),
        "exposes": ("maintain_team_data",)},
    "team-maintenance:schedule": {
        "capabilities": frozenset({IDENTITY, EVIDENCE, TEAM, SKILL, DELEGATE, OUTPUT}),
        "exposes": ("maintain_team_data",)},
    "team-maintenance:standings": {
        "capabilities": frozenset({IDENTITY, EVIDENCE, TEAM, SKILL, DELEGATE, OUTPUT}),
        "exposes": ("maintain_team_data",)},
    "team-maintenance:profile": {
        "capabilities": frozenset({IDENTITY, EVIDENCE, TEAM, SKILL, DELEGATE, OUTPUT}),
        "exposes": ("maintain_team_data",)},
    "team-maintenance:portraits": {
        "capabilities": frozenset({IDENTITY, EVIDENCE, TEAM, PERSON, SKILL, DELEGATE, OUTPUT}),
        "exposes": ("fetch_player_photos", "review_maintenance")},
    # The names block reads no article either — its work is one lookup per
    # player plus the same paid-judgement residue the review direction handles —
    # but it still keeps EVIDENCE so a child can be handed the tool at all.
    "team-maintenance:names": {
        "capabilities": frozenset({IDENTITY, EVIDENCE, TEAM, PERSON, SKILL, DELEGATE, OUTPUT}),
        "exposes": ("maintain_team_data", "review_maintenance")},

    # --- team: panel leaves ----------------------------------------------------
    # The parent holds TEAM_WORK and delegates. A leaf only subtracts. Leaves
    # that call maintain_team_data or fetch_player_photos keep EVIDENCE, because
    # spawn_agent refuses those tools on a child that cannot read what they
    # return. stats.charts does not fetch; it only reads samples already stored.
    "team:club.crest": {
        "capabilities": frozenset({IDENTITY, EVIDENCE, TEAM, SKILL, OUTPUT}),
        "exposes": ("maintain_team_data",)},
    "team:squad.info": {
        "capabilities": frozenset({IDENTITY, EVIDENCE, TEAM, PERSON, SKILL, OUTPUT}),
        "exposes": ("maintain_team_data",)},
    # Translation is the leaf itself. It does not look names up, so it does not
    # get maintain_team_data (that part still searches Wikidata) or the photo tools.
    "team:squad.names": {
        "capabilities": frozenset({IDENTITY, TEAM, PERSON, SKILL, OUTPUT})},
    "team:squad.images": {
        "capabilities": frozenset({IDENTITY, EVIDENCE, TEAM, PERSON, SKILL, OUTPUT}),
        "exposes": ("fetch_player_photos", "review_maintenance")},
    "team:schedule.standings": {
        "capabilities": frozenset({IDENTITY, EVIDENCE, TEAM, SKILL, OUTPUT}),
        "exposes": ("maintain_team_data",)},
    "team:stats.charts": {
        "capabilities": frozenset({IDENTITY, TEAM, SKILL, OUTPUT})},
}


def topic_declared(skill, topic):
    """What a loaded direction narrows to, or None when it declares nothing.

    ``None`` means "keep the skill's set": a direction that has not declared its
    own narrowing is not a claim that it needs less.
    """
    return TOPIC_CAPABILITIES.get(f"{skill}:{topic}")


def topic_exposes(skill, topic):
    """Background tools a loaded direction asks to have offered, if any.

    Kept separate from the capability set so that opening one of these can never
    be read as widening what the task may do — the capability set is still the
    intersection, and this is only about whether an already-relevant tool is
    advertised.
    """
    entry = topic_declared(skill, topic) or {}
    return tuple(entry.get("exposes") or ())


# A workflow that *is* the maintenance pass, rather than a conversation about
# one, needs the background tools before it has loaded any direction. The topic
# mechanism above cannot help it: the root reads the skill, decides which blocks
# are stale, and only then hands them out — so at the moment it is assembling
# its tool surface, no topic is loaded yet, and the tools it must delegate are
# still withheld. A sub-agent can only be given tools its parent holds, so
# withholding them at the root means the whole tree has nothing to run.
#
# The same justification the image direction uses applies here and is stronger:
# a maintenance run exists to run these batches, so a tool that runs one is not
# an extra capability, it is the task itself. Scope still decides permission
# afterwards — demo mode, no network, a fixed article or match all still remove
# them — and nothing here can override that.
WORKFLOW_EXPOSES = {
    "team_refresh": ("maintain_team_data", "fetch_player_photos", "review_maintenance"),
    # The panel parent decides which leaves to spawn before any topic is loaded,
    # and a child can only receive tools the parent holds. Scope still removes
    # these afterwards. Per-leaf narrowing is the topic table above.
    "team": ("maintain_team_data", "fetch_player_photos", "review_maintenance"),
}


def workflow_exposes(workflow):
    """Background tools a workflow asks to have offered before any topic loads."""
    return tuple(WORKFLOW_EXPOSES.get(str(workflow)) or ())

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
    # Read-only and local, unlike the two background photo tools: it costs no
    # network wait, so a conversation may use it to see what is missing before
    # asking for a batch.
    "photo_queue": (TEAM, PERSON),
    "plan_photo_batch": (PERSON,),
    # Local read costs nothing. The judgement round behind it is a paid role, and
    # it spans people (names, roster facts) and the club record (fields, crest),
    # so it carries both capabilities rather than only PERSON.
    "review_maintenance": (PERSON, TEAM),
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
