"""Tool surface for an agentic portrait leaf under ``squad.images``.

The model chooses the next action. Deterministic tools fetch and match, and
they attach a photo only when the action's own proof is present. The model
cannot supply a photo URL, and it cannot lift a namesake birth-date refusal.

This module is the prototype. It is not registered on the conversation tool
list: ``fetch_player_photos`` remains the pass an operator runs, and that pass
does not call the free name search. A later step can expose ``apply_action``
once the leaf is the procedure the model is asked to follow.

``community_idespn`` does not issue ``searchplayers.php``. The free API has no
reverse lookup by ESPN id, and discovering a record by name is the failed path.
"""
from __future__ import annotations

from .ingestion import now
from .team_portraits import DIRECTORIES, espn_headshot

ACTIONS = (
    "read_gaps",
    "espn_headshot",
    "wikidata_p18",
    "club_directory",
    "wikimedia_club",
    "community_idespn",
    "give_up",
)

# Closed set. Free text from the model is not a reason code.
GIVE_UP_REASONS = frozenset({
    "no_identity_proof",
    "namesake_blocked",
    "source_gap",
    "license_not_free",
    "club_site_unconfigured",
    "stuck",
})

# What each attaching action is allowed to write. A tool that returns any
# other ``photo_match`` is refused, so a stub or a model cannot sneak a face
# in through the wrong proof.
ACTION_PROOFS = {
    "espn_headshot": "espn_provider_id",
    "wikidata_p18": "wikidata_espn_provider_id",
    "club_directory": "exact_name_and_number",
    "wikimedia_club": "wikimedia_verified_club",
}

_PROOF_FIELDS = ("photo_url", "photo_credit", "photo_source_url", "photo_file_page",
                 "photo_license", "photo_match")


def _refuse(action):
    """Reject a payload that tries to be the source instead of a command."""
    if not isinstance(action, dict):
        return "action_not_object"
    if any(key in action for key in ("photo_url", "url", "href", "image")):
        return "model_cannot_supply_url"
    if action.get("override_namesake") or action.get("accept_namesake"):
        return "namesake_birthdate_refusal"
    name = action.get("action")
    if name not in ACTIONS:
        return "unknown_action"
    if name == "give_up" and action.get("reason") not in GIVE_UP_REASONS:
        return "give_up_reason_required"
    return ""


def read_gaps(roster):
    """Local gaps only. No image URL leaves this view."""
    gaps = []
    for row in roster or []:
        if row.get("photo_url"):
            continue
        gaps.append({
            "provider_id": str(row.get("provider_id") or ""),
            "name": row.get("name") or "",
            "namesake_blocked": bool(row.get("namesake_blocked")),
            "reason_code": row.get("reason_code") or "",
            "has_headshot_payload": isinstance(row.get("headshot"), dict),
        })
    return gaps


def _attach(player, photo, proof):
    if photo.get("photo_match") != proof or not photo.get("photo_url"):
        return False
    if not all(photo.get(field) for field in _PROOF_FIELDS if field != "photo_file_page" or proof != "espn_provider_id"):
        # ESPN roster headshots carry credit, source and licence, not a Commons file page.
        if proof != "espn_provider_id":
            return False
        if not all(photo.get(field) for field in ("photo_url", "photo_credit", "photo_source_url", "photo_license", "photo_match")):
            return False
    player.update({field: photo[field] for field in _PROOF_FIELDS if field in photo})
    player["photo_updated_at"] = photo.get("fetched_at") or photo.get("photo_updated_at") or now()
    player.pop("photo_reason", None)
    player.pop("namesake_blocked", None)
    player.pop("reason_code", None)
    player.pop("retryable", None)
    return True


async def apply_action(snapshot, player, action, tools=None):
    """Run one model command. Returns a trace step and never invents a URL."""
    tools = tools or {}
    refusal = _refuse(action)
    if refusal:
        return {"ok": False, "attached": False, "reason_code": refusal, "action": (action or {}).get("action") if isinstance(action, dict) else ""}
    name = action["action"]
    if name == "read_gaps":
        return {"ok": True, "attached": False, "action": name, "gaps": read_gaps(snapshot.get("roster"))}
    if not isinstance(player, dict):
        return {"ok": False, "attached": False, "action": name, "reason_code": "unknown_player"}
    provider_id = str(player.get("provider_id") or "")
    if name == "give_up":
        if not player.get("photo_url"):
            player["photo_reason"] = "代理核对停止：" + action["reason"]
            player["reason_code"] = "agent_gave_up"
            player["retryable"] = action["reason"] in {"stuck", "source_gap"}
        return {"ok": True, "attached": False, "action": name, "provider_id": provider_id,
                "reason_code": action["reason"]}
    if name == "community_idespn":
        # The free publisher cannot be asked "who has this ESPN id". Asking by
        # name is the failed path, so this command stops without a request.
        return {"ok": True, "attached": False, "action": name, "provider_id": provider_id,
                "reason_code": "community_name_search_retired",
                "photo_reason": "免费社区库不能按球员编号反查，姓名搜索已退出，本动作不会发出检索。"}
    if name == "espn_headshot":
        photo = espn_headshot({"id": provider_id, "headshot": player.get("headshot")}, fetched_at=now())
        attached = bool(photo) and _attach(player, photo, ACTION_PROOFS[name])
        return {"ok": True, "attached": attached, "action": name, "provider_id": provider_id,
                "reason_code": "" if attached else "espn_headshot_absent",
                "photo_match": player.get("photo_match") if attached else ""}
    if name == "club_directory":
        team_id = str(snapshot.get("team_id") or "")
        if team_id not in DIRECTORIES:
            return {"ok": True, "attached": False, "action": name, "provider_id": provider_id,
                    "reason_code": "club_site_unconfigured",
                    "photo_reason": "这家俱乐部还没有经过人工复核的姓名加号码目录，不能从页面上猜一张脸。"}
        finder = tools.get("club_directory")
        photo = await finder(player) if finder else {}
        attached = bool(photo) and _attach(player, photo, ACTION_PROOFS[name])
        return {"ok": True, "attached": attached, "action": name, "provider_id": provider_id,
                "reason_code": "" if attached else "club_directory_miss",
                "photo_match": player.get("photo_match") if attached else ""}
    finder = tools.get(name)
    if finder is None:
        return {"ok": False, "attached": False, "action": name, "provider_id": provider_id,
                "reason_code": "tool_not_configured"}
    photo = await finder(player)
    if not isinstance(photo, dict):
        photo = {}
    if photo.get("status") == "unavailable":
        return {"ok": True, "attached": False, "action": name, "provider_id": provider_id,
                "reason_code": photo.get("reason_code") or "source_unavailable"}
    attached = photo.get("status") == "completed" and _attach(player, photo, ACTION_PROOFS[name])
    return {"ok": True, "attached": attached, "action": name, "provider_id": provider_id,
            "reason_code": "" if attached else (photo.get("reason_code") or "no_proof"),
            "photo_match": player.get("photo_match") if attached else ""}


def score_trace(trace, roster):
    """Count what the loop did. An attach without a proof match is a failure."""
    attached = [step for step in trace if step.get("attached")]
    proofs = set(ACTION_PROOFS.values())
    by_id = {str(row.get("provider_id") or ""): row for row in roster or []}
    illegal = 0
    for step in attached:
        row = by_id.get(step.get("provider_id"))
        if not row or row.get("photo_match") not in proofs or not row.get("photo_url"):
            illegal += 1
    switched = 0
    previous = {}
    for step in trace:
        identity = step.get("provider_id")
        if identity and previous.get(identity) and previous[identity] != step.get("action") and not step.get("attached"):
            switched += 1
        if identity:
            previous[identity] = step.get("action")
    return {
        "actions": len(trace),
        "attached_with_proof": len(attached) - illegal,
        "illegal_attach": illegal,
        "refused_invention": sum(step.get("reason_code") in {"model_cannot_supply_url", "namesake_birthdate_refusal"}
                                 for step in trace),
        "namesake_held": sum(bool(row.get("namesake_blocked")) and not row.get("photo_url") for row in by_id.values()),
        "gave_up": sum(step.get("action") == "give_up" and step.get("ok") for step in trace),
        "retired_name_search": sum(step.get("reason_code") == "community_name_search_retired" for step in trace),
        "switched_after_miss": switched,
    }


async def run_actions(snapshot, actions, tools=None):
    """Apply a scripted sequence. The caller is the stand-in for the model."""
    roster = {str(row.get("provider_id") or ""): row for row in snapshot.get("roster") or []}
    trace = []
    for action in actions:
        identity = str((action or {}).get("provider_id") or "") if isinstance(action, dict) else ""
        player = roster.get(identity) if identity else None
        trace.append(await apply_action(snapshot, player, action, tools))
    return snapshot, trace, score_trace(trace, snapshot.get("roster"))
