"""Football task adapter over the domain-independent agent task runtime.

Child transcripts and results live only for the current task tree. Shared source
storage keeps its existing provenance, privacy and transient-body rules.
"""
from __future__ import annotations

import copy
import hashlib

from .agent_tools import ToolError


def _schema(properties, required=()):
    return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}


_ID = {"type": "string", "minLength": 1, "maxLength": 120}
_IDS = {"type": "array", "items": _ID, "minItems": 1, "maxItems": 12}
_CURSOR = {"type": "integer", "minimum": 0, "maximum": 9007199254740991}
DELEGATION_TOOLS = [
    {"name": "spawn_agent", "description": "创建独立子智能体，立即返回编号。可连续创建多个以并行研究或整理；只传必要背景，子结论不能代替原文证据。",
     "parameters": _schema({"task": {"type": "string", "minLength": 1, "maxLength": 4000},
                            "context": {"type": "string", "maxLength": 8000},
                            "skill": {"type": "string", "maxLength": 80},
                            "evidence_ids": {"type": "array", "items": _ID, "maxItems": 40},
                            "tools": {"type": "array", "items": {"type": "string", "maxLength": 80}, "minItems": 1, "maxItems": 40}}, ["task"])},
    {"name": "list_agents", "description": "查看自己直接创建的子智能体状态和候选结果。结果中的来源须自行阅读后引用。",
     "parameters": _schema({"agent_ids": _IDS, "after_cursor": _CURSOR})},
    {"name": "wait_agents", "description": "等待指定子智能体的新进展，最先有进展即可返回；超时不会停止子任务。继续等待时传上次cursor，避免重复轮询。",
     "parameters": _schema({"agent_ids": _IDS, "after_cursor": _CURSOR,
                            "timeout_seconds": {"type": "integer", "minimum": 0, "maximum": 60}})},
    {"name": "send_agent", "description": "给直接子智能体补充任务。运行中在安全节点接收；已完成或失败则开启新一轮，原总时限不延长。返回送达确认，不代表任务完成。",
     "parameters": _schema({"agent_id": _ID, "message": {"type": "string", "minLength": 1, "maxLength": 8000}}, ["agent_id", "message"])},
    {"name": "cancel_agent", "description": "停止不再需要的直接子智能体及其全部后代，等待清理完毕。",
     "parameters": _schema({"agent_id": _ID}, ["agent_id"])},
    {"name": "release_agent", "description": "已取得所需结果且不再继续时，释放已结束子任务的上下文和结果。释放后无法继续该实例，可另建任务。",
     "parameters": _schema({"agent_id": _ID}, ["agent_id"])},
]
DELEGATION_NAMES = {tool["name"] for tool in DELEGATION_TOOLS}
GUIDANCE = """
你可以按需要使用子智能体。互相独立的研究、资料比较或多篇消息整理适合并行；简单问题可自行完成。
spawn_agent只创建并立即返回，可在同一轮创建多个。主任务可以继续研究，也可wait_agents等待结果。
子智能体不自动看到本会话，须明确给出目标、必要背景和交付要求；不要复制无关历史或整篇原文，优先传evidence_ids供其读取。
子智能体与主任务共用受限模型请求额度，等待不占额度。可用send_agent补充或继续任务，cancel_agent停止无用分支。
子结果属于候选分析，不是本次已读原文，也不是独立来源。父任务引用前必须read_evidence实际阅读，最终仍遵守本任务的核验和提交规则。
使用上次返回的cursor等待新增进展，不反复list_agents轮询；部分子任务失败时可继续处理其他结果。最终交付前处理必要结果，剩余分支会被自动停止。
已取得所需结果且不再需要继续的子任务，可用release_agent释放上下文，避免大量已完成任务长期占用内存。
"""


def base_store(store):
    return getattr(store, "_delegate_base", store)


def runtime_for(store):
    store = base_store(store)
    runtime = getattr(store, "_agent_runtime", None)
    if runtime is None:
        from .agent_runtime import AgentRuntime
        runtime = AgentRuntime(observer=lambda record: store.put("agent_tasks", record))
        store._agent_runtime = runtime
    return runtime


def recover_tasks(store):
    """Metadata survives restart, private continuations deliberately do not."""
    for row in store.list("agent_tasks"):
        if row.get("status") not in {"completed", "failed", "cancelled", "interrupted"}:
            store.put("agent_tasks", {**row, "status": "interrupted", "error": "服务重启，子任务上下文已释放，请重新发起主任务。"})


def source_version(item):
    if not item:
        return None
    return {"fingerprint": item.get("body_hash") or hashlib.sha256(str(item.get("text") or "").encode()).hexdigest(),
            **{key: item.get(key) for key in ("team_id", "mode", "title", "url", "source_name", "published_at", "fetched_at", "origin")}}


def checked_versions(local, evidence_ids):
    versions = {}
    for eid in evidence_ids:
        if not local.cache_is_current(eid):
            raise ToolError("引用原文或阅读范围在处理期间发生变化，请重新读取并核对后再交付。")
        versions[eid] = source_version(local.evidence_view(local.scoped_get("evidence", eid)))
    return versions


class ChildStore:
    """Only this child's run is overlaid; coordination/cache state stays shared."""
    def __init__(self, store, run, settings):
        object.__setattr__(self, "_delegate_base", base_store(store))
        object.__setattr__(self, "_run", copy.deepcopy(run))
        object.__setattr__(self, "_settings", copy.deepcopy(settings))

    def __getattr__(self, name):
        return getattr(self._delegate_base, name)

    def __setattr__(self, name, value):
        if name in {"_delegate_base", "_run", "_settings"}:
            object.__setattr__(self, name, value)
        else:
            setattr(self._delegate_base, name, value)

    def get(self, collection, identifier):
        if collection == "runs":
            return copy.deepcopy(self._run) if identifier == self._run["id"] else None
        return self._delegate_base.get(collection, identifier)

    def put(self, collection, item):
        if collection == "runs":
            if item["id"] != self._run["id"]:
                raise ToolError("子任务不能写入其他任务。")
            self._run = copy.deepcopy(item)
            return copy.deepcopy(item)
        return self._delegate_base.put(collection, item)

    def list(self, collection, mode=None, team_id=None):
        if collection == "runs":
            return [copy.deepcopy(self._run)] if ((mode is None or mode == self._run["mode"])
                and (team_id is None or team_id == self._run["team_id"])) else []
        return self._delegate_base.list(collection, mode=mode, team_id=team_id)

    def settings(self):
        return copy.deepcopy(self._settings)


class Delegation:
    definitions = DELEGATION_TOOLS

    def __init__(self, store, run, local, settings, cancel_event, deadline, *, execution=None):
        self.store, self.run, self.local = store, run, local
        self.settings = copy.deepcopy(settings)
        self.runtime = runtime_for(store)
        self.execution = execution
        self.id = execution.id if execution else run["id"]
        self.root_id = execution.root_id if execution else run["id"]
        if execution is None:
            self.runtime.register_root(self.id, cancel_event=cancel_event, deadline=deadline)

    def inbox(self):
        return self.runtime.drain_inbox(self.id)

    def has_messages(self):
        return bool(self.runtime.has_pending_messages(self.id))

    def _result_view(self, response):
        response = copy.deepcopy(response)
        for row in response.get("agents", []):
            result = row.get("result")
            if not isinstance(result, dict):
                continue
            for eid, version in result.get("source_versions", {}).items():
                current = self.local.evidence_view(self.local.scoped_get("evidence", eid))
                if not current or source_version(current) != version:
                    row.update(result=None, result_unavailable=True, error="子任务引用的原文或范围已变化，请重新查阅；旧结论不再返回。")
                    break
        response["notice"] = "子任务结论是候选材料，不是原文证据；引用前须自行读取来源。"
        return response

    @staticmethod
    def _acknowledgement(response):
        # Commands acknowledge an operation; only list/wait may return source-
        # checked candidates. Retrying an old creation must not leak its result.
        return {key: value for key, value in response.items() if key != "result"}

    async def call(self, name, args, request_id=None):
        from .agent_runtime import RuntimeAccessError, RuntimeCapacityError, RuntimeStateError
        try:
            if name == "spawn_agent":
                from .agent import SKILLS
                from .skill_catalog import resolve_workflow
                try:
                    skill = resolve_workflow(args.get("skill", "ask"))
                except ValueError:
                    raise ToolError("子任务技能不存在。") from None
                if skill not in SKILLS:
                    raise ToolError("子任务技能不存在。")
                if skill in {"match_preview", "match_review"} and skill != self.local.scope_skill:
                    raise ToolError("子任务技能不能改变当前比赛的赛前或复盘范围。")
                allowed = {tool["function"]["name"] for tool in self.local.available_tools()}
                requested = set(args.get("tools", allowed))
                if not requested <= allowed:
                    raise ToolError("子任务工具只能从当前任务可用工具中选择。")
                requested.add("submit_answer")
                evidence_ids = args.get("evidence_ids", self.run.get("input_evidence_ids", []))
                for eid in evidence_ids:
                    if not self.local.evidence_allowed(self.local.scoped_get("evidence", eid)):
                        raise ToolError("子任务来源不在当前可见范围。")
                if evidence_ids and "read_evidence" not in requested:
                    raise ToolError("子任务指定了来源，需要保留 read_evidence 阅读工具。")
                if requested & {"get_team_data", "maintain_team_data", "read_article", "research_news"} and "read_evidence" not in requested:
                    raise ToolError("所选复合工具会读取原文，需要同时选择 read_evidence 阅读工具。")
                payload = {"task": args["task"], "context": args.get("context", ""), "skill": skill,
                           "evidence_ids": list(dict.fromkeys(evidence_ids)), "tools": sorted(requested)}
                return self._acknowledgement(self.runtime.spawn(self.id, execute=self._execute, payload=payload, request_id=request_id))
            if name == "send_agent":
                return self._acknowledgement(self.runtime.send(self.id, args["agent_id"], args["message"], request_id=request_id))
            if name == "cancel_agent":
                return self._acknowledgement(await self.runtime.cancel(self.id, args["agent_id"]))
            if name == "release_agent":
                return self.runtime.release(self.id, args["agent_id"])
            ids, cursor = args.get("agent_ids"), args.get("after_cursor", 0)
            if name == "list_agents":
                return self._result_view(self.runtime.inspect(self.id, ids, after=cursor))
            if name == "wait_agents":
                if "after_cursor" not in args:
                    initial = self.runtime.inspect(self.id, ids)
                    if not initial["agents"] or any(row["status"] in {"completed", "failed", "cancelled"} for row in initial["agents"]):
                        return self._result_view(initial)
                    cursor = initial["cursor"]
                result = await self.runtime.wait(self.id, ids, after=cursor, timeout=args.get("timeout_seconds", 30))
                return self._result_view(result)
            raise ToolError("未知子任务操作。")
        except (RuntimeAccessError, RuntimeCapacityError, RuntimeStateError) as exc:
            raise ToolError(str(exc)) from None

    async def _execute(self, context):
        from .agent import run_agent, now
        payload = context.payload
        child = {key: copy.deepcopy(self.run.get(key)) for key in ("mode", "team_id", "match_id", "event_id", "report_id", "model_role", "provider_id", "model", "allow_network")}
        # Preserve the parent's actual scope even when a different workflow is used.
        child.update(id=context.id, internal_agent=True, scope_skill=self.run.get("scope_skill", self.run["skill"]),
                     allow_network=self.local.allow_network,
                     input_evidence_ids=payload["evidence_ids"], scope_evidence_ids=self.run.get("scope_evidence_ids", self.run.get("input_evidence_ids", [])),
                     skill=payload["skill"], question=payload["task"], delegation_context=payload["context"],
                     tool_allowlist=payload["tools"], conversation_id=context.id,
                     status="queued", answer="", evidence_ids=[], warnings=[], steps=[], error=None,
                     created_at=now(), finished_at=None, agent_generation=context.generation)
        prior = context.previous_result
        if isinstance(prior, dict):
            prior_view = self._result_view({"agents": [{"result": prior}]})["agents"][0]
            if prior_view.get("result"):
                child["delegation_previous"] = prior["answer"]
        settings = copy.deepcopy(self.settings)
        # Children supply candidates; the owning task enforces final verification.
        settings["verification_enabled"] = False
        child_store = ChildStore(self.store, child, settings)
        await run_agent(child_store, context.id, context.cancel_event, execution=context, settings_override=settings)
        result = child_store.get("runs", context.id)
        if result["status"] == "cancelled":
            import asyncio
            raise asyncio.CancelledError()
        if result["status"] != "completed":
            from .provider import AgentFailure
            raise AgentFailure(result.get("error") or "子任务未能完成。")
        return {key: copy.deepcopy(result.get(key)) for key in ("answer", "evidence_ids", "evidence_versions", "delivery")} | {
            "source_versions": copy.deepcopy(result.get("evidence_versions", {})),
            "notice": "候选结果，尚未作为主任务结论交付。"}

    async def close(self, status):
        if self.execution is None:
            await self.runtime.finish_root(self.id, status)
