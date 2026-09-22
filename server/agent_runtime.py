"""Process-local task trees, independent of applications, models, and persistence.

All methods run on one asyncio event loop. Roots represent externally managed
tasks; only descendants are started by this runtime. Results and inboxes live
until explicitly released or the root finishes. Diagnostics contain metadata.
"""
from __future__ import annotations

import asyncio
import copy
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import math
import time
from typing import Any, Callable
import uuid


class AgentRuntimeError(RuntimeError):
    """A safe, application-independent runtime contract error."""


class RuntimeStateError(AgentRuntimeError):
    pass


class RuntimeAccessError(AgentRuntimeError):
    pass


class RuntimeCapacityError(AgentRuntimeError):
    pass


@dataclass(frozen=True)
class AgentContext:
    id: str
    parent_id: str | None
    root_id: str
    generation: int
    deadline: float
    cancel_event: asyncio.Event
    payload: Any
    previous_result: Any

    def __getitem__(self, key):
        return getattr(self, key)


@dataclass
class _Task:
    id: str
    parent_id: str | None
    root_id: str
    deadline: float
    cancel_event: asyncio.Event
    depth: int = 0
    generation: int = 1
    status: str = "queued"
    execute: Callable | None = None
    payload: Any = None
    result: Any = None
    error: str | None = None
    children: list[str] = field(default_factory=list)
    inbox: deque = field(default_factory=deque)
    task: asyncio.Task | None = None
    stopping: bool = False
    message_sequence: int = 0
    context_released: bool = False
    payload_bytes: int = 0
    result_bytes: int = 0
    inbox_bytes: int = 0
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class AgentRuntime:
    """One event-loop runtime with a synchronous, metadata-only observer."""
    TERMINAL = frozenset({"completed", "failed", "cancelled", "interrupted"})
    MAX_JSON_BYTES = 1_000_000
    MAX_CONTEXT_BYTES = 16_000_000
    MAX_INBOX_MESSAGES = 128

    def __init__(self, observer=None, max_active_per_root=12, max_depth=3):
        if max_active_per_root < 1 or max_depth < 1:
            raise ValueError("Runtime limits must be positive")
        self.observer = observer
        self.max_active_per_root = max_active_per_root
        self.max_depth = max_depth
        self._nodes = {}
        self._events = {}
        self._signals = {}
        self._cursors = {}
        self._requests = {}
        self._context_bytes = {}
        self._monitors = {}
        self._finishers = {}
        self._cancellations = set()
        self._recent = deque(maxlen=256)
        self._closed = False

    def _json_copy(self, value):
        try:
            serialized = json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True)
            if len(serialized.encode("utf-8")) > self.MAX_JSON_BYTES:
                raise RuntimeCapacityError("Runtime value exceeds the in-memory size limit")
            return json.loads(serialized)
        except (TypeError, ValueError, RecursionError):
            raise RuntimeStateError("Runtime values must be finite JSON data") from None

    def _node(self, task_id):
        node = self._nodes.get(task_id)
        if node is None:
            raise RuntimeAccessError("Task is not available in this runtime")
        return node

    def _live(self, task_id):
        node = self._node(task_id)
        root = self._node(node.root_id)
        if (self._closed or root.stopping or node.stopping or node.context_released
                or node.status in self.TERMINAL or root.cancel_event.is_set()
                or node.cancel_event.is_set() or time.monotonic() >= node.deadline):
            raise RuntimeStateError("Task is stopped or its deadline has elapsed")
        return node

    def _owned(self, parent_id, task_id):
        self._node(parent_id)
        child = self._node(task_id)
        if child.parent_id != parent_id:
            raise RuntimeAccessError("Only a direct parent can manage this task")
        return child

    def _metadata(self, node):
        return {"id": node.id, "parent_id": node.parent_id, "root_id": node.root_id,
                "generation": node.generation, "status": node.status,
                "created_at": node.created_at, "error": node.error,
                "context_released": node.context_released}

    def _public(self, node):
        return {**self._metadata(node), "result": copy.deepcopy(node.result)}

    def _emit(self, node, kind="status"):
        root_id = node.root_id
        self._cursors[root_id] += 1
        event = {**self._metadata(node), "sequence": self._cursors[root_id], "kind": kind}
        self._events[root_id].append(event)
        # Replace before waking: waiters capture the old signal before checking
        # the cursor, so a transition cannot be lost between check and subscribe.
        signal = self._signals[root_id]
        self._signals[root_id] = asyncio.Event()
        signal.set()
        if self.observer is not None:
            try:
                self.observer(copy.deepcopy(event))
            except Exception:
                # An optional diagnostic sink must not change task execution.
                pass

    def register_root(self, task_id, *, cancel_event, deadline):
        if self._closed:
            raise RuntimeStateError("Runtime has shut down")
        if not isinstance(task_id, str) or not task_id or not math.isfinite(deadline):
            raise RuntimeStateError("A root requires an ID and a finite deadline")
        if task_id in self._nodes:
            old = self._nodes[task_id]
            if old.parent_id is None and old.cancel_event is cancel_event and old.deadline == deadline:
                return self._public(old)
            raise RuntimeStateError("Task ID is already registered")
        root = _Task(task_id, None, task_id, deadline, cancel_event, status="running")
        self._nodes[task_id] = root
        self._events[task_id] = deque(maxlen=512)
        self._signals[task_id] = asyncio.Event()
        self._cursors[task_id] = 0
        self._requests[task_id] = {}
        self._context_bytes[task_id] = 0
        self._emit(root)
        self._monitors[task_id] = asyncio.create_task(self._watch_root(root))
        return self._public(root)

    async def _watch_root(self, root):
        try:
            try:
                await asyncio.wait_for(root.cancel_event.wait(), max(0, root.deadline - time.monotonic()))
                error = "cancelled"
            except TimeoutError:
                error = "deadline_exceeded"
                root.cancel_event.set()
            root.stopping = True
            await self._stop_descendants(root)
            if root.id in self._nodes:
                root.status = "failed" if error == "deadline_exceeded" else "cancelled"
                root.error = error
                self._emit(root)
        except asyncio.CancelledError:
            raise

    def _capacity(self, root_id):
        active = sum(node.parent_id is not None and node.root_id == root_id
                     and node.status not in self.TERMINAL for node in self._nodes.values())
        if active >= self.max_active_per_root:
            raise RuntimeCapacityError("Active child task capacity is full; wait for a task to finish")

    @staticmethod
    def _json_size(value):
        return len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8"))

    def _reserve_context(self, root_id, additional):
        if self._context_bytes[root_id] + additional > self.MAX_CONTEXT_BYTES:
            raise RuntimeCapacityError("Task-tree context memory is full; release consumed completed tasks first")
        self._context_bytes[root_id] += additional

    def _request(self, parent, operation, request_id, value):
        if request_id is None:
            return None, None
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 200:
            raise RuntimeStateError("Request ID must contain between 1 and 200 characters")
        signature = hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        key = (parent.id, operation, request_id)
        previous = self._requests[parent.root_id].get(key)
        if previous is not None:
            if previous[0] != signature:
                raise RuntimeStateError("Request ID was already used with different arguments")
            return key, self._public(self._node(previous[1]))
        return key, signature

    def spawn(self, parent_id, *, execute, payload, request_id=None):
        parent = self._live(parent_id)
        payload = self._json_copy(payload)
        callback_id = [id(getattr(execute, "__func__", execute)), id(getattr(execute, "__self__", None))]
        key, signature = self._request(parent, "spawn", request_id, {"payload": payload, "callback": callback_id})
        if isinstance(signature, dict):
            return signature
        if not callable(execute):
            raise RuntimeStateError("A child requires an execution callback")
        if parent.depth >= self.max_depth:
            raise RuntimeCapacityError("Task nesting depth has reached its limit")
        self._capacity(parent.root_id)
        payload_bytes = self._json_size(payload)
        self._reserve_context(parent.root_id, payload_bytes)
        child = _Task("agent_" + uuid.uuid4().hex, parent.id, parent.root_id,
                      parent.deadline, asyncio.Event(), depth=parent.depth + 1,
                      execute=execute, payload=payload, payload_bytes=payload_bytes)
        self._nodes[child.id] = child
        parent.children.append(child.id)
        if key is not None:
            self._requests[parent.root_id][key] = (signature, child.id)
        self._emit(child)
        child.task = asyncio.create_task(self._drive(child))
        return self._public(child)

    def send(self, parent_id, task_id, message, request_id=None):
        parent = self._live(parent_id)
        child = self._owned(parent_id, task_id)
        message = self._json_copy(message)
        key, signature = self._request(parent, "send", request_id, {"task_id": task_id, "message": message})
        if isinstance(signature, dict):
            return signature
        if child.context_released:
            raise RuntimeStateError("Task context was released; create a new task for further work")
        if child.stopping or child.cancel_event.is_set() or child.status in {"cancelled", "interrupted"}:
            raise RuntimeStateError("A stopped task cannot receive more work")
        if len(child.inbox) >= self.MAX_INBOX_MESSAGES:
            raise RuntimeCapacityError("Task inbox is full; allow it to consume pending messages")
        restart = child.status in self.TERMINAL
        if restart:
            self._capacity(child.root_id)
        message_bytes = self._json_size(message)
        self._reserve_context(child.root_id, message_bytes)
        child.inbox_bytes += message_bytes
        child.message_sequence += 1
        child.inbox.append({"sequence": child.message_sequence, "message": message})
        if key is not None:
            self._requests[parent.root_id][key] = (signature, child.id)
        if restart:
            child.generation += 1
            child.status, child.error = "queued", None
            child.task = asyncio.create_task(self._drive(child))
        self._emit(child, "message")
        return self._public(child)

    def drain_inbox(self, task_id):
        node = self._node(task_id)
        messages = list(node.inbox)
        node.inbox.clear()
        self._context_bytes[node.root_id] -= node.inbox_bytes
        node.inbox_bytes = 0
        return copy.deepcopy(messages)

    def pending_count(self, task_id):
        return len(self._node(task_id).inbox)

    def has_pending_messages(self, task_id):
        return self.pending_count(task_id) > 0

    async def _drive(self, node):
        while True:
            initial_message_sequence = node.message_sequence
            try:
                self._live(node.id)
                node.status, node.error = "running", None
                self._emit(node)
                context = AgentContext(node.id, node.parent_id, node.root_id, node.generation,
                                       node.deadline, node.cancel_event, copy.deepcopy(node.payload),
                                       copy.deepcopy(node.result))
                async with asyncio.timeout(max(0, node.deadline - time.monotonic())):
                    result = await node.execute(context)
                self._live(node.id)
                result = self._json_copy(result)
                result_bytes = self._json_size(result)
                self._reserve_context(node.root_id, result_bytes - node.result_bytes)
                node.result_bytes = result_bytes
                status, error = "completed", None
            except asyncio.CancelledError:
                status, error, result = "cancelled", "cancelled", None
                node.stopping = True
                node.cancel_event.set()
            except TimeoutError:
                status, error, result = "failed", "execution_timeout", None
            except RuntimeCapacityError:
                status, error, result = "failed", "context_capacity_exceeded", None
            except Exception:
                status, error, result = "failed", "execution_failed", None
            try:
                await self._stop_descendants(node)
            except asyncio.CancelledError:
                node.stopping = True
                node.cancel_event.set()
                status, error, result = "cancelled", "cancelled", None
                await self._stop_descendants(node)
            root = self._node(node.root_id)
            if time.monotonic() >= node.deadline:
                status, error, result = "failed", "deadline_exceeded", None
                node.stopping = True
                node.cancel_event.set()
            elif node.stopping or root.stopping or node.cancel_event.is_set() or root.cancel_event.is_set():
                status, error, result = "cancelled", "cancelled", None
                node.stopping = True
                node.cancel_event.set()
            unread = bool(node.inbox) and not node.stopping and not self._node(node.root_id).cancel_event.is_set()
            new_messages = unread and node.message_sequence > initial_message_sequence
            if unread and not new_messages:
                status, error, result = "failed", "unconsumed_messages", None
            if result is None:
                self._context_bytes[node.root_id] -= node.result_bytes
                node.result_bytes = 0
            node.result, node.error = result, error
            if new_messages:
                node.generation += 1
                node.status = "queued"
                self._emit(node)
                await asyncio.sleep(0)
                continue
            node.status = status
            self._emit(node)
            return

    def inspect(self, parent_id, task_ids=None, after=0):
        parent = self._node(parent_id)
        if not isinstance(after, int) or after < 0:
            raise RuntimeStateError("Event cursor must be a nonnegative integer")
        ids = list(parent.children) if task_ids is None else list(dict.fromkeys(task_ids))
        children = [self._owned(parent_id, child_id) for child_id in ids]
        selected = set(ids)
        history = self._events[parent.root_id]
        events = [copy.deepcopy(event) for event in history
                  if event["sequence"] > after and event["parent_id"] == parent_id and event["id"] in selected]
        return {"agents": [self._public(child) for child in children], "events": events,
                "cursor": self._cursors[parent.root_id], "timed_out": False,
                "events_truncated": bool(history and after < history[0]["sequence"] - 1)}

    async def wait(self, parent_id, task_ids=None, after=0, timeout=30):
        parent = self._node(parent_id)
        if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout < 0:
            raise RuntimeStateError("Wait timeout must be a finite nonnegative number")
        stop = min(parent.deadline, time.monotonic() + timeout)
        while True:
            signal = self._signals[parent.root_id]
            current = self.inspect(parent_id, task_ids, after)
            if (current["events"] or current["events_truncated"]
                    or all(row["status"] in self.TERMINAL for row in current["agents"])):
                return current
            remaining = stop - time.monotonic()
            if remaining <= 0:
                current["timed_out"] = True
                return current
            try:
                await asyncio.wait_for(signal.wait(), remaining)
            except TimeoutError:
                current = self.inspect(parent_id, task_ids, after)
                current["timed_out"] = not bool(current["events"] or current["events_truncated"])
                return current

    def _subtree(self, node):
        result = []
        for child_id in node.children:
            child = self._nodes[child_id]
            result.append(child)
            result.extend(self._subtree(child))
        return result

    def _mark_stopping(self, nodes):
        current = asyncio.current_task()
        tasks = []
        for node in nodes:
            if node.status in self.TERMINAL and (node.task is None or node.task.done()):
                continue
            already_stopping = node.stopping
            node.stopping = True
            node.cancel_event.set()
            if node.task is not None and node.task is not current and not node.task.done():
                if not already_stopping:
                    node.task.cancel()
                tasks.append(node.task)
        return tasks

    async def _settle_stopped(self, nodes, tasks):
        if tasks:
            # Stopping a cleanup waiter must not inject a second cancellation
            # into callbacks already releasing resources in their finally blocks.
            await asyncio.shield(asyncio.gather(*tasks, return_exceptions=True))
        # A task cancelled before its first instruction never enters _drive.
        for node in nodes:
            if node.status not in self.TERMINAL and node.task is not asyncio.current_task():
                self._context_bytes[node.root_id] -= node.result_bytes
                node.result_bytes = 0
                node.status, node.error, node.result = "cancelled", "cancelled", None
                self._emit(node)

    async def _stop_nodes(self, nodes):
        await self._settle_stopped(nodes, self._mark_stopping(nodes))

    async def _stop_descendants(self, node):
        await self._stop_nodes(self._subtree(node))

    async def cancel(self, parent_id, task_id):
        node = self._owned(parent_id, task_id)
        nodes = [node, *self._subtree(node)]
        # Request cancellation synchronously, before a queued child can start.
        # Retain the complete convergence task even if this caller disappears.
        tasks = self._mark_stopping(nodes)
        cleanup = asyncio.create_task(self._settle_stopped(nodes, tasks))
        self._cancellations.add(cleanup)
        cleanup.add_done_callback(self._cancellations.discard)
        await asyncio.shield(cleanup)
        return self._public(node)

    def release(self, parent_id, task_id):
        """Explicitly discard a consumed terminal subtree, retaining ID tombstones."""
        self._live(parent_id)
        node = self._owned(parent_id, task_id)
        nodes = [node, *self._subtree(node)]
        if any(item.status not in self.TERMINAL for item in nodes):
            raise RuntimeStateError("Only a completed or stopped subtree can release its context")
        for item in nodes:
            self._context_bytes[item.root_id] -= item.payload_bytes + item.result_bytes + item.inbox_bytes
            item.payload_bytes = item.result_bytes = item.inbox_bytes = 0
            item.payload = item.result = item.execute = item.task = None
            item.inbox.clear()
            item.context_released = True
            self._emit(item, "released")
        return self._public(node)

    async def finish_root(self, task_id, status):
        pending = self._finishers.get(task_id)
        if pending is not None:
            await asyncio.shield(pending)
            return
        root = self._nodes.get(task_id)
        if root is None:
            return
        if root.parent_id is not None:
            raise RuntimeAccessError("Only an external root can be finished explicitly")
        if status not in self.TERMINAL:
            raise RuntimeStateError("Root completion requires a terminal status")
        root.stopping = True
        cleanup = asyncio.create_task(self._finish_root(root, status))
        self._finishers[task_id] = cleanup
        try:
            await asyncio.shield(cleanup)
        finally:
            if cleanup.done():
                self._finishers.pop(task_id, None)

    async def _finish_root(self, root, status):
        task_id = root.id
        monitor = self._monitors.pop(task_id, None)
        if monitor is not None and monitor is not asyncio.current_task():
            monitor.cancel()
            await asyncio.gather(monitor, return_exceptions=True)
        await self._stop_descendants(root)
        root.status = status
        self._emit(root)
        for node in [root, *self._subtree(root)]:
            self._recent.append(self._metadata(node))
            del self._nodes[node.id]
        self._signals.pop(task_id).set()
        self._events.pop(task_id)
        self._cursors.pop(task_id)
        self._requests.pop(task_id)
        self._context_bytes.pop(task_id)
        self._finishers.pop(task_id, None)

    async def shutdown(self):
        self._closed = True
        for root_id in list(self._events):
            await self.finish_root(root_id, "interrupted")
        if self._cancellations:
            await asyncio.shield(asyncio.gather(*self._cancellations, return_exceptions=True))

    def snapshot(self):
        return copy.deepcopy({"limits": {"active_per_root": self.max_active_per_root,
                                          "max_depth": self.max_depth},
                              "active": [self._metadata(node) for node in self._nodes.values()],
                              "recent": list(self._recent)})
