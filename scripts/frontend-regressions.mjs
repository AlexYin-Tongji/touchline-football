import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import vm from "node:vm";
import ts from "typescript";

// Execute the actual components with a small hook host. No server, browser, or
// provider is contacted; event handlers and lifecycle cleanup remain production code.
function component(file, name, props, api, saved = {}) {
  const slots = [];
  const effects = [];
  const intervals = new Map();
  let index = 0, dirty = true, tree, alive = true;
  const same = (a, b) => a && b && a.length === b.length && a.every((x, i) => Object.is(x, b[i]));
  const react = {
    lazy: loader => ({ lazy: loader }),
    Suspense: "Suspense",
    useState(initial) {
      const i = index++;
      if (!(i in slots)) slots[i] = typeof initial === "function" ? initial() : initial;
      return [slots[i], value => {
        const next = typeof value === "function" ? value(slots[i]) : value;
        if (!Object.is(slots[i], next)) { slots[i] = next; dirty = true; }
      }];
    },
    useRef(value) { const i = index++; return slots[i] ??= { current: value }; },
    useCallback(fn, deps) {
      const i = index++;
      if (!same(slots[i]?.deps, deps)) slots[i] = { fn, deps };
      return slots[i].fn;
    },
    useEffect(fn, deps) {
      const i = index++;
      if (!same(slots[i]?.deps, deps)) effects.push(() => {
        slots[i]?.cleanup?.();
        slots[i] = { deps, cleanup: fn() };
      });
    },
  };
  const jsx = (type, props) => ({ type, props });
  const exports = {};
  let intervalId = 0;
  const browser = { setInterval: (fn, delay) => { const id = ++intervalId; intervals.set(id, { fn, delay }); return id; }, setTimeout: () => 1, addEventListener() {}, removeEventListener() {} };
  const document = { visibilityState: "visible", ...browser };
  vm.runInNewContext(ts.transpileModule(readFileSync(new URL(`../src/${file}`, import.meta.url), "utf8"), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2022 },
  }).outputText, {
    exports, Date, Set, JSON, console, URL, window: browser,
    document, clearInterval: id => intervals.delete(id), clearTimeout() {},
    localStorage: { getItem: key => saved[key] || "", setItem: (key, value) => { saved[key] = value; } },
    require(module) {
      if (module === "react") return react;
      if (module === "react/jsx-runtime") return { jsx, jsxs: jsx, Fragment: "Fragment" };
      if (module === "./api") return { api: path => api(path, "GET"), post: (path, body) => api(path, "POST", body), errorText: e => e.message };
      if (module === "./v7Shared") return { uniqueImages: images => images || [], friendlyTime: () => "", Markdown: "Markdown", Photo: "Photo", Sources: "Sources" };
      if (module === "./components") return { dateTime: () => "", safeUrl: value => value || null, Crest: "Crest", Modal: "Modal", Spinner: "Spinner" };
      if (module === "./clubCatalog") return { findClub: () => null };
      if (module === "./teamHub" || module === "./clubNames") return { clubName: x => x };
      return new Proxy({}, { get: (_, key) => String(key) });
    },
  });
  return {
    get tree() { return tree; }, saved,
    rerender(next) { props = { ...props, ...next }; dirty = true; },
    setVisibility(value) { document.visibilityState = value; },
    tickIntervals(delay) { for (const timer of intervals.values()) if (timer.delay === delay) timer.fn(); },
    async flush() {
      for (let turn = 0; turn < 12; turn++) {
        await Promise.resolve();
        if (dirty && alive) {
          dirty = false; index = 0;
          tree = exports[name](props);
          while (effects.length) effects.shift()();
        }
      }
    },
    unmount() { alive = false; for (const slot of slots) slot?.cleanup?.(); },
  };
}
function nodes(node, predicate, found = []) {
  if (!node || typeof node !== "object") return found;
  if (predicate(node)) found.push(node);
  if (Array.isArray(node)) node.forEach(n => nodes(n, predicate, found));
  else if (node.props) nodes(node.props.children, predicate, found);
  return found;
}
function text(node) {
  if (node == null) return "";
  if (typeof node !== "object") return String(node);
  return Array.isArray(node) ? node.map(text).join("") : text(node.props?.children);
}
const button = (host, label) => nodes(host.tree, n => n.type === "button" && text(n).includes(label))[0];
const deferred = () => { let resolve; const promise = new Promise(r => { resolve = r; }); return { promise, resolve }; };
const team = { id: "arsenal", name: "阿森纳" };
const data = { matches: [], evidence: [], setup: { model_configured: true }, settings: { llm: { model: "available" } } };
const modelState = { providers: [{ id: "provider", name: "Local mock", models: [{ id: "available", name: "available" }] }], active: { provider_id: "provider", model: "available" } };
const workflowState = (changes = {}) => ({
  kind: "news", team_id: team.id, enabled: false, status: "idle", phase: "idle",
  message: "启动后持续跟进球队动态。", last_error: null, started_at: null,
  finished_at: null, next_run_at: null, updated_at: null,
  progress: { completed: 0, failed: 0, remaining: 0 },
  memory: { summary: "", next_steps: [], completed_count: 0 }, ...changes,
});

test("feed uses workflow updates without losing loaded pages or showing old retry banners", async () => {
  const paths = [];
  const items = Array.from({ length: 40 }, (_, index) => ({ id: String(index), title: `消息${index}`, summary: "正文", team_id: team.id }));
  const host = component("v7Feed.tsx", "FeedView", { team, data, ask() {}, openModel() {} }, path => {
    paths.push(path);
    const url = new URL(path, "http://local");
    const cursor = url.searchParams.get("cursor");
    const limit = Number(url.searchParams.get("limit"));
    return Promise.resolve({ items: cursor ? items.slice(20) : items.slice(0, limit), next_cursor: !cursor && limit < 40 ? "older" : null,
      processing: { failed: 8, note: "8条未整理成功，请手动重试" } });
  });
  await host.flush();
  assert.equal(button(host, "手动整理"), undefined);
  assert.equal(button(host, "刷新来源"), undefined);
  assert.doesNotMatch(text(host.tree), /8条未整理成功/);
  button(host, "查看更多动态").props.onClick();
  await host.flush();
  const controls = nodes(host.tree, node => node.type === "WorkflowControls")[0];
  assert.ok(controls);
  controls.props.onChange(workflowState({ enabled: true, status: "waiting" }));
  await host.flush();
  assert.equal(nodes(host.tree, node => node.type === "article").length, 40);
  assert.equal(new URL(paths.at(-1), "http://local").searchParams.get("limit"), "40");
  host.unmount();
});

test("workflow starts, waits, stops and restores persisted active state after remount", async () => {
  let current = workflowState();
  const calls = [], changed = [];
  const request = (path, method) => {
    calls.push([path, method]);
    if (method === "POST") current = path.endsWith("/start")
      ? workflowState({ enabled: true, status: "running", message: "正在整理本轮消息。" })
      : workflowState({ status: "stopped", message: "已结束，已有内容保留。" });
    return Promise.resolve(current);
  };
  const props = { kind: "news", teamId: team.id, onChange: state => changed.push(state.status) };
  const host = component("WorkflowControls.tsx", "WorkflowControls", props, request);
  await host.flush();
  assert.equal(button(host, "启动").props.disabled, false);
  assert.equal(button(host, "结束").props.disabled, true);
  await button(host, "启动").props.onClick();
  await host.flush();
  assert.equal(button(host, "启动").props.disabled, true);
  assert.equal(button(host, "结束").props.disabled, false);
  current = workflowState({ enabled: true, status: "waiting", message: "本轮完成，等待下一轮。", progress: { completed: 3, failed: 0, remaining: 0 } });
  host.tickIntervals(4000);
  await host.flush();
  assert.match(text(host.tree), /本轮完成/);
  assert.match(text(host.tree), /已完成 3/);
  host.unmount();
  const restored = component("WorkflowControls.tsx", "WorkflowControls", props, request);
  await restored.flush();
  assert.equal(button(restored, "启动").props.disabled, true);
  assert.equal(button(restored, "结束").props.disabled, false);
  await button(restored, "结束").props.onClick();
  await restored.flush();
  assert.match(text(restored.tree), /已有内容保留/);
  assert.equal(button(restored, "启动").props.disabled, false);
  assert.deepEqual(calls.filter(([, method]) => method === "POST").map(([path]) => path), ["/workflows/news/arsenal/start", "/workflows/news/arsenal/stop"]);
  assert.deepEqual(changed, ["running", "waiting", "stopped"]);
  restored.unmount();
});

test("workflow ignores slow GET and POST responses after switching teams", async () => {
  for (const slowMethod of ["GET", "POST"]) {
    const slow = deferred(), changes = [];
    const host = component("WorkflowControls.tsx", "WorkflowControls", { kind: "news", teamId: "arsenal", onChange: state => changes.push(state.team_id) }, (path, method) => {
      if (path.includes("arsenal") && method === slowMethod) return slow.promise;
      return Promise.resolve(workflowState(path.includes("liverpool") ? { team_id: "liverpool", enabled: true, status: "waiting", message: "利物浦等待下一轮" } : {}));
    });
    await host.flush();
    let action;
    if (slowMethod === "POST") action = button(host, "启动").props.onClick();
    host.rerender({ teamId: "liverpool" });
    await host.flush();
    slow.resolve(workflowState({ enabled: true, status: "running", message: "阿森纳旧响应" }));
    await action;
    await host.flush();
    assert.match(text(host.tree), /利物浦等待下一轮/);
    assert.doesNotMatch(text(host.tree), /阿森纳旧响应/);
    assert.equal(button(host, "结束").props.disabled, false);
    assert.deepEqual(changes, []);
    host.unmount();
  }
});

test("workflow polls only visible pages and rejects an old GET after stop", async () => {
  const late = deferred();
  let gets = 0;
  const host = component("WorkflowControls.tsx", "WorkflowControls", { kind: "team", teamId: team.id }, (path, method) => {
    if (method === "POST") return Promise.resolve(workflowState({ kind: "team", status: "stopped", message: "已结束更新" }));
    gets++;
    return gets === 2 ? late.promise : Promise.resolve(workflowState({ kind: "team", enabled: true, status: "running", message: "正在更新" }));
  });
  await host.flush();
  host.setVisibility("hidden");
  host.tickIntervals(4000);
  await host.flush();
  assert.equal(gets, 1);
  host.setVisibility("visible");
  host.tickIntervals(4000);
  await host.flush();
  await button(host, "结束").props.onClick();
  await host.flush();
  late.resolve(workflowState({ kind: "team", enabled: true, status: "running", message: "过期的更新状态" }));
  await host.flush();
  assert.match(text(host.tree), /已结束更新/);
  assert.equal(button(host, "启动").props.disabled, false);
  host.unmount();
});

test("workflow shows model configuration failure without pretending work started", async () => {
  const host = component("WorkflowControls.tsx", "WorkflowControls", { kind: "news", teamId: team.id, onConfigure() {} }, (path, method) =>
    method === "POST" ? Promise.reject(new Error("请先为球队动态配置模型。")) : Promise.resolve(workflowState()));
  await host.flush();
  await button(host, "启动").props.onClick();
  await host.flush();
  assert.match(text(host.tree), /请先为球队动态配置模型/);
  assert.equal(button(host, "启动").props.disabled, false);
  assert.equal(button(host, "结束").props.disabled, true);
  assert.ok(button(host, "模型设置"));
  host.unmount();
});

test("workflow shows current source failures before any report exists but hides inactive historical errors", async () => {
  for (const [status, enabled, failed, visible] of [
    ["waiting", true, 0, true],
    ["running", true, 0, true],
    ["blocked", false, 0, true],
    ["failed", false, 0, true],
    ["interrupted", false, 0, true],
    ["stopped", false, 8, false],
    ["idle", false, 8, false],
  ]) {
    const current = workflowState({ status, enabled, last_error: "本轮来源暂不可读，尚未取得正文。",
      progress: { completed: 0, failed, remaining: 0 } });
    const host = component("WorkflowControls.tsx", "WorkflowControls", { kind: "news", teamId: team.id }, () => Promise.resolve(current));
    await host.flush();
    const alerts = nodes(host.tree, node => node.props?.role === "alert");
    assert.equal(text(alerts).includes("本轮来源暂不可读"), visible, `${status}: failure visibility follows the current workflow, not report counts`);
    host.unmount();
  }
});

test("workflow commands completing after unmount cannot publish status changes", async () => {
  const slow = deferred(), changes = [];
  const host = component("WorkflowControls.tsx", "WorkflowControls", {
    kind: "news", teamId: team.id, onChange: state => changes.push(state.status),
  }, (path, method) => method === "POST" ? slow.promise : Promise.resolve(workflowState()));
  await host.flush();
  const action = button(host, "启动").props.onClick();
  host.unmount();
  slow.resolve(workflowState({ enabled: true, status: "running" }));
  await action;
  assert.deepEqual(changes, []);
});

test("team center reads only and preserves existing players while workflow changes refresh its snapshot", async () => {
  const next = deferred(), calls = [];
  const center = { team, roster: [{ id: "player", name: "已有球员", position: "中场", position_group: "midfielder" }],
    matches: [], stats: {}, sources: [] };
  const host = component("v7TeamCenter.tsx", "TeamCenter", { team, data: { ...data, teams: [team] }, ask() {} }, (path, method) => {
    calls.push([path, method]);
    return calls.length === 1 ? Promise.resolve(center) : next.promise;
  });
  await host.flush();
  button(host, "球员阵容").props.onClick();
  await host.flush();
  assert.match(text(host.tree), /已有球员/);
  assert.equal(button(host, "更新阵容"), undefined);
  assert.equal(nodes(host.tree, node => node.props?.["aria-label"] === "更新球队资料").length, 0);
  nodes(host.tree, node => node.type === "WorkflowControls")[0].props.onChange(workflowState({ kind: "team", status: "stopped" }));
  await host.flush();
  assert.match(text(host.tree), /已有球员/, "pending reads do not clear an existing snapshot");
  next.resolve(center);
  await host.flush();
  assert.match(text(host.tree), /已有球员/);
  assert.ok(calls.every(([path, method]) => path === "/teams/arsenal/center" && method === "GET"));
  host.unmount();
});

test("visible shell polling refreshes match context using summary only and pauses while hidden", async () => {
  let matchId = "initial";
  const paths = [];
  const host = component("App.tsx", "default", {}, path => {
    paths.push(path);
    return Promise.resolve({ ...data, teams: [team], settings: { ...data.settings, followed_teams: [team.id] }, matches: [{ id: matchId, team_id: team.id }] });
  }, { "touchline-team": team.id });
  await host.flush();
  assert.equal(paths.length, 1, "initial rendering makes one summary request");
  matchId = "updated-by-background-maintenance";
  host.tickIntervals(60000);
  await host.flush();
  assert.equal(nodes(host.tree, node => node.type === "FeedView")[0].props.data.matches[0].id, matchId);
  assert.equal(paths.length, 2);
  host.setVisibility("hidden");
  host.tickIntervals(60000);
  await host.flush();
  assert.equal(paths.length, 2, "do not poll a hidden page");
  assert.ok(paths.every(path => new URL(path, "http://local").searchParams.get("view") === "summary"));
  host.unmount();
});

test("returning to feed or assistant refreshes newly maintained matches without repeated loads", async () => {
  let matchId = "initial", calls = 0;
  const host = component("App.tsx", "default", {}, () => {
    calls++;
    return Promise.resolve({ ...data, teams: [team], settings: { ...data.settings, followed_teams: [team.id] }, matches: [{ id: matchId, team_id: team.id }] });
  }, { "touchline-team": team.id, "touchline-page": "team" });
  await host.flush();
  assert.equal(calls, 1);
  matchId = "new-next-match";
  button(host, "动态").props.onClick();
  await host.flush();
  assert.equal(nodes(host.tree, node => node.type === "FeedView")[0].props.data.matches[0].id, matchId);
  assert.equal(calls, 2);
  matchId = "new-finished-match";
  button(host, "问助手").props.onClick();
  await host.flush();
  assert.equal(nodes(host.tree, node => node.props?.incoming !== undefined)[0].props.data.matches[0].id, matchId);
  assert.equal(calls, 3, "page state and summary response must not trigger a load loop");
  host.unmount();
});

test("a report follow-up cannot navigate or overwrite drafts after leaving its team", async () => {
  const report = deferred(), asks = [];
  const host = component("v7Feed.tsx", "FeedView", { team, data, ask: value => asks.push(value), openModel() {} }, path =>
    path.startsWith("/reports/") ? report.promise : Promise.resolve({ items: [{ id: "news", title: "新闻", summary: "内容", report_id: "report", team_id: team.id }], next_cursor: null }));
  await host.flush();
  const pending = button(host, "问问助手").props.onClick();
  host.unmount();
  report.resolve({ status: "completed", report: { title: "新闻", sources: [{ id: "source" }] } });
  await pending;
  assert.equal(asks.length, 0);
});

test("draft edits made while sending survive the successful response and local persistence", async () => {
  const response = deferred();
  const host = component("v7Assistant.tsx", "Assistant", { team, data, incoming: null, active: true, openModel() {}, onChanged: async () => {} }, (path, method) => {
    if (method === "POST") return response.promise;
    return Promise.resolve(path === "/model-providers" ? modelState : { conversations: [] });
  }, { "touchline-draft-arsenal": "问题 A" });
  await host.flush();
  nodes(host.tree, n => n.type === "form")[0].props.onSubmit({ preventDefault() {} });
  await host.flush();
  nodes(host.tree, n => n.type === "textarea")[0].props.onChange({ target: { value: "接着写的问题 B" } });
  response.resolve({ id: "run", conversation_id: "conversation", status: "queued", provider_id: "provider", model: "available" });
  await host.flush();
  assert.equal(nodes(host.tree, n => n.type === "textarea")[0].props.value, "接着写的问题 B");
  assert.equal(host.saved["touchline-draft-arsenal"], "接着写的问题 B");
  host.unmount();
});

test("selecting a feed topic loads that topic from the first server page", async () => {
  const paths = [];
  const host = component("v7Feed.tsx", "FeedView", { team, data, ask() {}, openModel() {} }, path => {
    paths.push(path);
    return Promise.resolve({ items: [], next_cursor: "older" });
  });
  await host.flush();
  button(host, "查看更多动态").props.onClick();
  await host.flush();
  button(host, "伤停").props.onClick();
  await host.flush();
  const url = new URL(paths.at(-1), "http://local");
  assert.equal(url.searchParams.get("topic"), "injury");
  assert.equal(url.searchParams.has("cursor"), false);
  host.unmount();
});

test("an unavailable conversation model offers a new conversation without discarding the draft", async () => {
  const host = component("v7Assistant.tsx", "Assistant", { team, data, incoming: null, active: true, openModel() {}, onChanged: async () => {} }, path => Promise.resolve(
    path === "/model-providers" ? modelState : path === "/conversations/saved" ? {
      conversation: { id: "saved", team_id: team.id, provider_id: "removed", model: "removed-model" },
      runs: [{ id: "old", question: "旧问题", answer: "旧回答", status: "completed", provider_id: "removed", model: "removed-model" }],
    } : { conversations: [] }), {
      "touchline-conversation-arsenal": "saved", "touchline-draft-arsenal": "继续的问题",
    });
  await host.flush();
  const recovery = button(host, "保留问题，开启新对话");
  assert.ok(recovery, "show an actionable recovery for an unavailable bound model");
  recovery.props.onClick();
  await host.flush();
  assert.equal(nodes(host.tree, n => n.type === "textarea")[0].props.value, "继续的问题");
  assert.equal(host.saved["touchline-conversation-arsenal"], "");
  host.unmount();
});
