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
      if (module === "./components") return { dateTime: () => "", Crest: "Crest", Modal: "Modal", Spinner: "Spinner" };
      if (module === "./clubCatalog") return { findClub: () => null };
      if (module === "./teamHub" || module === "./clubNames") return { clubName: x => x };
      return new Proxy({}, { get: (_, key) => String(key) });
    },
  });
  return {
    get tree() { return tree; }, saved,
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
