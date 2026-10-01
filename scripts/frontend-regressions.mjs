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
    memo: value => value,
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
      if (module === "./components") return { dateTime: () => "", safeUrl: value => value || null, Crest: "Crest", Modal: "Modal", SourceGapNotice: "SourceGapNotice", Spinner: "Spinner" };
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
  message: "点击启动，更新一次球队动态。", last_error: null, started_at: null,
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
  controls.props.onChange(workflowState({ status: "completed" }));
  await host.flush();
  assert.equal(nodes(host.tree, node => node.type === "article").length, 40);
  assert.equal(new URL(paths.at(-1), "http://local").searchParams.get("limit"), "40");
  host.unmount();
});

test("partial and unavailable news reviews keep the full story with one notice and the original source", async () => {
  for (const reviewStatus of ["partial", "unavailable"]) {
    const reviewNotice = reviewStatus === "partial" ? "部分重点信息尚未完成核对，请结合来源阅读。" : "重点信息核对暂不可用，请结合来源阅读。";
    const source = { id: "source", title: "原始报道", source_name: "公开媒体", url: "https://example.com/original-news" };
    const report = { status: "completed", report: { title: "完整球队新闻", lead: "完整导语仍可阅读。",
      sections: ["开头的事实", "中间的条件和归因", "结尾的重要进展"].map((body, index) => ({ heading: `第${index + 1}部分`, body, evidence_ids: [source.id] })),
      sources: [source], review_status: reviewStatus, review_notice: reviewNotice } };
    const host = component("v7Feed.tsx", "FeedView", { team, data, ask() {}, openModel() {} }, path =>
      Promise.resolve(path.startsWith("/reports/") ? report : { items: [{ id: "news", title: "完整球队新闻", summary: "摘要", report_id: "report", team_id: team.id }], next_cursor: null }));
    await host.flush();
    button(host, "展开阅读").props.onClick();
    await host.flush();
    const reader = nodes(host.tree, node => node.type === "article" && node.props.className === "story-reader")[0];
    assert.ok(reader);
    assert.match(text(reader), /完整导语仍可阅读/);
    assert.deepEqual(nodes(reader, node => node.type === "Markdown").map(node => node.props.text), ["开头的事实", "中间的条件和归因", "结尾的重要进展"]);
    assert.equal(text(reader).split(reviewNotice).length - 1, 1);
    assert.equal(nodes(reader, node => node.props?.role === "note").length, 1);
    assert.doesNotMatch(text(reader), /已全面验证|重点信息已核对/);
    const sources = nodes(reader, node => node.type === "Sources").flatMap(node => node.props.sources || []);
    assert.ok(sources.length > 0);
    assert.ok(sources.every(item => item.url === "https://example.com/original-news"));
    host.unmount();
  }
});

test("feed flags a partially read report and shows its source gap in the story", async () => {
  const gaps = [{ evidence_id: "hidden-in-ui", source_name: "卫报体育", host: "www.theguardian.com",
    code: "timeout", stage: "read", message: "来源请求超时" }];
  const report = { status: "completed", report: { title: "球队新闻", lead: "已核对的内容", sections: [],
    sources: [{ id: "public", source_name: "球队官网", url: "https://example.com/news" }], source_gaps: gaps } };
  const host = component("v7Feed.tsx", "FeedView", { team, data, ask() {}, openModel() {} }, path =>
    Promise.resolve(path.startsWith("/reports/") ? report : { items: [{ id: "news", title: "球队新闻", summary: "已核对的内容",
      report_id: "report", team_id: team.id, source_gap_count: 1 }], next_cursor: null }));
  await host.flush();
  const card = nodes(host.tree, node => node.type === "article" && node.props.className === "feed-card")[0];
  assert.match(text(card), /1 个来源/);
  button(host, "展开阅读").props.onClick();
  await host.flush();
  const reader = nodes(host.tree, node => node.type === "article" && node.props.className === "story-reader")[0];
  assert.deepEqual(nodes(reader, node => node.type === "SourceGapNotice").map(node => node.props.gaps), [gaps]);
  host.unmount();
});

test("source gap notice explains a failed publisher without exposing evidence identifiers", async () => {
  const gap = { evidence_id: "private-reference", source_name: "卫报体育", host: "www.theguardian.com",
    code: "timeout", stage: "read", message: "来源请求超时" };
  const forbidden = { evidence_id: "private-reference-2", source_name: "另一来源", host: "example.com",
    code: "http_forbidden", stage: "read", message: "来源返回 HTTP 403", http_status: 403 };
  const unknown = { evidence_id: "private-reference-3", source_name: "未知来源",
    code: "unknown", message: "https://private.example/token?api_key=hidden" };
  const host = component("components.tsx", "SourceGapNotice", { gaps: [gap, forbidden, unknown] }, () => Promise.resolve(null));
  await host.flush();
  assert.match(text(host.tree), /卫报体育/);
  assert.match(text(host.tree), /超时/);
  assert.match(text(host.tree), /拒绝访问/);
  assert.equal(text(host.tree).split("HTTP 403").length - 1, 1);
  assert.doesNotMatch(text(host.tree), /private-reference/);
  assert.doesNotMatch(text(host.tree), /api_key=hidden/);
  assert.equal(nodes(host.tree, node => node.type === "a").length, 0);
  host.unmount();
});

test("run view shows structured source failure alongside completed answer", async () => {
  const run = { id: "run", mode: "live", team_id: team.id, conversation_id: "conversation", skill: "ask",
    question: "球队消息", status: "completed", answer: "已确认的消息", evidence_ids: [],
    source_errors: [{ evidence_id: "private-reference", source_name: "卫报体育", host: "www.theguardian.com",
      code: "timeout", stage: "read", message: "来源请求超时" }] };
  const host = component("components.tsx", "RunView", { run, evidence: [], openEvidence() {}, onCancel() {}, onRetry() {}, onSettings() {}, cancelling: false }, () => Promise.resolve(null));
  await host.flush();
  assert.deepEqual(nodes(host.tree, node => node.type?.name === "SourceGapNotice").map(node => node.props.gaps), [run.source_errors]);
  host.unmount();
});

test("workflow completes one manual update, stays idle, and can start and stop again", async () => {
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
  current = workflowState({ status: "completed", message: "本次更新已完成。", progress: { completed: 3, failed: 0, remaining: 0 } });
  host.tickIntervals(4000);
  await host.flush();
  assert.match(text(host.tree), /本次更新已完成/);
  assert.match(text(host.tree), /已完成 3/);
  assert.equal(button(host, "启动").props.disabled, false);
  assert.equal(button(host, "结束").props.disabled, true);
  const completedCalls = calls.length;
  host.tickIntervals(30000);
  host.tickIntervals(4000);
  await host.flush();
  assert.equal(calls.length, completedCalls);
  await button(host, "启动").props.onClick();
  await host.flush();
  host.unmount();
  const restored = component("WorkflowControls.tsx", "WorkflowControls", props, request);
  await restored.flush();
  assert.equal(button(restored, "启动").props.disabled, true);
  assert.equal(button(restored, "结束").props.disabled, false);
  await button(restored, "结束").props.onClick();
  await restored.flush();
  assert.match(text(restored.tree), /已有内容保留/);
  assert.equal(button(restored, "启动").props.disabled, false);
  assert.deepEqual(calls.filter(([, method]) => method === "POST").map(([path]) => path), ["/workflows/news/arsenal/start", "/workflows/news/arsenal/start", "/workflows/news/arsenal/stop"]);
  assert.deepEqual(changed, ["running", "completed", "running", "stopped"]);
  restored.unmount();
});

test("workflow ignores slow GET and POST responses after switching teams", async () => {
  for (const slowMethod of ["GET", "POST"]) {
    const slow = deferred(), changes = [];
    const host = component("WorkflowControls.tsx", "WorkflowControls", { kind: "news", teamId: "arsenal", onChange: state => changes.push(state.team_id) }, (path, method) => {
      if (path.includes("arsenal") && method === slowMethod) return slow.promise;
      return Promise.resolve(workflowState(path.includes("liverpool") ? { team_id: "liverpool", enabled: true, status: "running", message: "利物浦正在更新" } : {}));
    });
    await host.flush();
    let action;
    if (slowMethod === "POST") action = button(host, "启动").props.onClick();
    host.rerender({ teamId: "liverpool" });
    await host.flush();
    slow.resolve(workflowState({ enabled: true, status: "running", message: "阿森纳旧响应" }));
    await action;
    await host.flush();
    assert.match(text(host.tree), /利物浦正在更新/);
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
  const gaps = [{ source_name: "天空体育", code: "http_forbidden", http_status: 403, message: "private-token-ignored" }];
  for (const [status, enabled, failed, visible] of [
    ["completed", false, 0, true],
    ["partial", false, 0, true],
    ["running", true, 0, true],
    ["blocked", false, 0, true],
    ["failed", false, 0, true],
    ["interrupted", false, 0, true],
    ["stopped", false, 8, false],
    ["idle", false, 8, false],
  ]) {
    const current = workflowState({ status, enabled, last_error: "本轮来源暂不可读，尚未取得正文。", source_errors: gaps,
      progress: { completed: 0, failed, remaining: 0 } });
    const host = component("WorkflowControls.tsx", "WorkflowControls", { kind: "news", teamId: team.id }, () => Promise.resolve(current));
    await host.flush();
    const alerts = nodes(host.tree, node => node.props?.role === "alert");
    assert.equal(text(alerts).includes("本轮来源暂不可读"), visible, `${status}: failure visibility follows the current workflow, not report counts`);
    assert.deepEqual(nodes(host.tree, node => node.type === "SourceGapNotice").map(node => node.props.gaps),
      visible ? [gaps] : [], `${status}: source diagnostics do not require a completed report`);
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
  assert.equal(nodes(host.tree, node => node.type === "DecisionHistory")[0].props.teamId, team.id);
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

test("team center labels the roster's actual season and explains a newer incomplete directory", async () => {
  const center = { team, season: { label: "2025/26" },
    roster_season: { id: "PL-2025", label: "2025/26", start_year: 2025 },
    latest_season: { id: "PL-2026", label: "2026/27", start_year: 2026 }, roster_fallback: true,
    roster: [{ id: "player", name: "已有球员", position: "中场", position_group: "MF" }],
    matches: [], stats: {}, sources: [] };
  const host = component("v7TeamCenter.tsx", "TeamCenter", { team, data: { ...data, teams: [team] }, ask() {} }, () => Promise.resolve(center));
  await host.flush();
  assert.match(text(host.tree), /当前为 2025\/26 赛季名单/);
  button(host, "球员阵容").props.onClick();
  await host.flush();
  assert.match(text(host.tree), /2026\/27 赛季球队目录尚未齐全/);
  host.unmount();

  const history = component("v7TeamCenter.tsx", "TeamCenter", { team, data: { ...data, teams: [team] }, ask() {} },
    () => Promise.resolve({ ...center, roster_fallback: false }));
  await history.flush();
  button(history, "球员阵容").props.onClick();
  await history.flush();
  assert.doesNotMatch(text(history.tree), /球队目录尚未齐全/);
  history.unmount();
});

test("decision history explains observations without exposing stored snapshots", async () => {
  const paths = [];
  const host = component("DecisionHistory.tsx", "DecisionHistory", { teamId: "arsenal" }, path => {
    paths.push(path);
    return Promise.resolve({ items: [{ id: "d1", created_at: "2026-09-28T02:00:00Z", finished_at: "2026-09-28T02:01:00Z",
      trigger: "automatic", status: "completed", reason: "阵容到期后重新读取。", summary: "来源读取完成。",
      season_label: "2025/26", parts: ["roster"], changes: [{ field: "roster", changed: false,
        confirmed: true, last_confirmed_at: "2026-09-28T02:01:00Z" }],
      before: { fingerprint: "private-fingerprint", values: ["private-before"] },
      after: { values: ["private-after"] } }] });
  });
  await host.flush();
  assert.deepEqual(paths, ["/teams/arsenal/decisions?limit=5"]);
  assert.match(text(host.tree), /更新记录/);
  assert.match(text(host.tree), /阵容到期后重新读取/);
  assert.match(text(host.tree), /阵容：已重新确认，无变化/);
  assert.doesNotMatch(text(host.tree), /private-/);
  host.unmount();
});

test("decision history ignores the previous team's late response and explains empty or failed reads", async () => {
  const old = deferred();
  let retry = false;
  const host = component("DecisionHistory.tsx", "DecisionHistory", { teamId: "arsenal" }, path => {
    if (path.includes("arsenal")) return old.promise;
    return retry ? Promise.resolve({ items: [], history_note: "仅记录启用后的观察。" }) : Promise.reject(new Error("暂时不可读"));
  });
  await host.flush();
  host.rerender({ teamId: "barcelona" });
  await host.flush();
  assert.match(text(host.tree), /暂时不可读/);
  retry = true;
  button(host, "重试").props.onClick();
  await host.flush();
  old.resolve({ items: [{ id: "old", created_at: "2026-09-28T02:00:00Z", trigger: "manual",
    status: "completed", reason: "旧球队记录", parts: [] }] });
  await host.flush();
  assert.match(text(host.tree), /仅记录启用后的观察/);
  assert.doesNotMatch(text(host.tree), /旧球队记录/);
  host.unmount();
});

test("decision history refreshes when opened or the team snapshot version changes", async () => {
  let reads = 0;
  const host = component("DecisionHistory.tsx", "DecisionHistory", { teamId: "arsenal", version: "one" }, () =>
    Promise.resolve({ items: [{ id: String(++reads), created_at: "2026-09-28T02:00:00Z",
      trigger: "manual", status: "completed", reason: `第 ${reads} 次观察`, parts: [] }] }));
  await host.flush();
  const details = nodes(host.tree, node => node.type === "details" && node.props?.className === "decision-history")[0];
  details.props.onToggle({ currentTarget: { open: true } });
  await host.flush();
  assert.match(text(host.tree), /第 2 次观察/);
  host.rerender({ version: "two" });
  await host.flush();
  assert.match(text(host.tree), /第 3 次观察/);
  host.unmount();
});

test("Wikimedia player photos need attribution while older sourced photos stay visible", async () => {
  const base = { id: "player", name: "测试球员", photo_url: "https://upload.wikimedia.org/player.jpg",
    photo_source_url: "https://en.wikipedia.org/wiki/Player", photo_match: "wikimedia_verified_club" };
  const missing = component("v7TeamCenter.tsx", "PlayerPhoto", { player: base }, () => Promise.resolve(null));
  await missing.flush();
  assert.equal(nodes(missing.tree, node => node.type === "img").length, 0);
  missing.unmount();
  const complete = component("v7TeamCenter.tsx", "PlayerPhoto", { player: { ...base,
    photo_credit: "Jane Doe", photo_license: "CC BY-SA 4.0", photo_file_page: "https://commons.wikimedia.org/wiki/File:Player.jpg" } }, () => Promise.resolve(null));
  await complete.flush();
  assert.equal(nodes(complete.tree, node => node.type === "img")[0].props.referrerPolicy, "no-referrer");
  complete.unmount();
  const legacy = component("v7TeamCenter.tsx", "PlayerPhoto", { player: { ...base, photo_match: "espn" } }, () => Promise.resolve(null));
  await legacy.flush();
  assert.equal(nodes(legacy.tree, node => node.type === "img").length, 1);
  legacy.unmount();
});

test("workflow clears the previous source diagnostics when a new update begins", async () => {
  const gaps = [{ source_name: "天空体育", code: "http_forbidden", http_status: 403 }];
  const host = component("WorkflowControls.tsx", "WorkflowControls", { kind: "news", teamId: team.id }, (_, method) =>
    Promise.resolve(method === "POST"
      ? workflowState({ enabled: true, status: "running", source_errors: [] })
      : workflowState({ status: "failed", last_error: "来源拒绝访问", source_errors: gaps })));
  await host.flush();
  assert.deepEqual(nodes(host.tree, node => node.type === "SourceGapNotice").map(node => node.props.gaps), [gaps]);
  await button(host, "启动").props.onClick();
  await host.flush();
  assert.equal(nodes(host.tree, node => node.type === "SourceGapNotice").length, 0);
  assert.doesNotMatch(text(host.tree), /来源拒绝访问/);
  host.unmount();
});

test("a failed sourced portrait can retry in details without adding buttons inside a player card", async () => {
  const player = { id: "player", name: "测试球员", photo_url: "https://example.com/player.jpg",
    photo_source_url: "https://example.com/players/player", photo_match: "exact_name_and_number" };
  const card = component("v7TeamCenter.tsx", "PlayerPhoto", { player }, () => Promise.resolve(null));
  await card.flush();
  nodes(card.tree, node => node.type === "img")[0].props.onError();
  await card.flush();
  assert.match(text(card.tree), /照片暂时无法加载/);
  assert.equal(nodes(card.tree, node => node.type === "button").length, 0);
  card.rerender({ player: { ...player, id: "next-player", photo_url: "https://example.com/next.jpg" } });
  await card.flush();
  assert.equal(nodes(card.tree, node => node.type === "img")[0].props.src, "https://example.com/next.jpg");
  card.unmount();
  const detail = component("v7TeamCenter.tsx", "PlayerPhoto", { player, allowRetry: true }, () => Promise.resolve(null));
  await detail.flush();
  nodes(detail.tree, node => node.type === "img")[0].props.onError();
  await detail.flush();
  button(detail, "重试照片").props.onClick();
  await detail.flush();
  assert.equal(nodes(detail.tree, node => node.type === "img")[0].props.src, player.photo_url);
  detail.unmount();
});

test("roster search finds Chinese and accented source names and resets for a different team", async () => {
  const roster = [
    { id: "joan", name: "Joan García", name_zh: "霍安·加西亚", position: "门将", position_group: "GK" },
    { id: "pedri", name: "Pedri", name_zh: "佩德里", position: "中场", position_group: "MF" },
  ];
  const center = { team, roster, matches: [], stats: {}, sources: [] };
  const host = component("v7TeamCenter.tsx", "TeamCenter", { team, data: { ...data, teams: [team] }, ask() {} }, () => Promise.resolve(center));
  await host.flush();
  button(host, "球员阵容").props.onClick();
  await host.flush();
  const search = () => nodes(host.tree, node => node.type === "input" && node.props.type === "search")[0];
  search().props.onChange({ target: { value: "garcia" } });
  await host.flush();
  const found = nodes(host.tree, node => node.props?.className === "player-card");
  assert.equal(found.length, 1);
  assert.match(text(found[0]), /霍安·加西亚Joan García/);
  search().props.onChange({ target: { value: "佩德里" } });
  await host.flush();
  assert.equal(nodes(host.tree, node => node.props?.className === "player-card").length, 1);
  button(host, "门将").props.onClick();
  await host.flush();
  assert.match(text(host.tree), /没有匹配的球员/);
  assert.doesNotMatch(text(host.tree), /还没有可读的阵容资料/);
  button(host, "查看全部球员").props.onClick();
  await host.flush();
  assert.equal(nodes(host.tree, node => node.props?.className === "player-card").length, 2);
  search().props.onChange({ target: { value: "佩德里" } });
  await host.flush();
  host.rerender({ team: { id: "barcelona", name: "巴塞罗那" } });
  await host.flush();
  button(host, "球员阵容").props.onClick();
  await host.flush();
  assert.equal(search().props.value, "");
  host.unmount();
});

test("visible team refresh preserves filters and updates or removes an open player with its source snapshot", async () => {
  let calls = 0;
  const player = { id: "player", name: "原球员", name_zh: "中文名", position: "中场", position_group: "MF" };
  let roster = [player];
  const host = component("v7TeamCenter.tsx", "TeamCenter", { team, data: { ...data, teams: [team] }, ask() {} }, () => {
    calls++;
    return Promise.resolve({ team, roster, matches: [], stats: {}, sources: [] });
  });
  await host.flush();
  button(host, "球员阵容").props.onClick();
  await host.flush();
  const search = () => nodes(host.tree, node => node.type === "input" && node.props.type === "search")[0];
  search().props.onChange({ target: { value: "中文名" } });
  await host.flush();
  button(host, "了解球员").props.onClick();
  await host.flush();
  host.setVisibility("hidden");
  host.tickIntervals(60000);
  await host.flush();
  assert.equal(calls, 1);
  roster = [{ ...player, photo_reason: "照片来源已撤回" }];
  host.setVisibility("visible");
  host.tickIntervals(60000);
  await host.flush();
  assert.equal(calls, 2);
  assert.equal(search().props.value, "中文名");
  assert.match(text(nodes(host.tree, node => node.props?.className === "player-detail")[0]), /照片来源已撤回/);
  roster = [];
  host.tickIntervals(60000);
  await host.flush();
  assert.equal(nodes(host.tree, node => node.props?.className === "player-detail").length, 0);
  host.unmount();
});

test("news source dates use Beijing time and distinguish an unknown publication date", async () => {
  const host = component("v7Shared.tsx", "Sources", { showDates: true, sources: [
    { name: "球队官网", url: "https://example.com/news", published_at: "2026-09-30T20:00:00Z" },
    { name: "另一媒体", url: "https://example.com/other" },
  ] }, () => Promise.resolve(null));
  await host.flush();
  const dates = nodes(host.tree, node => node.type === "time");
  assert.equal(dates.length, 1);
  assert.equal(text(dates[0]), "2026/10/1");
  assert.equal(dates[0].props.dateTime, "2026-09-30T20:00:00Z");
  assert.match(text(host.tree), /发布时间未知/);
  host.unmount();
});

test("player detail makes photographer license and file page readable", async () => {
  const player = { id: "player", name: "测试球员", position: "中场", position_group: "midfielder",
    photo_url: "https://upload.wikimedia.org/player.jpg", photo_source_url: "https://en.wikipedia.org/wiki/Player",
    photo_file_page: "https://commons.wikimedia.org/wiki/File:Player.jpg", photo_credit: "Jane Doe",
    photo_license: "CC BY-SA 4.0", photo_restrictions: "personality", photo_match: "wikimedia_verified_club" };
  const center = { team, roster: [player], matches: [], stats: {}, sources: [] };
  const host = component("v7TeamCenter.tsx", "TeamCenter", { team, data: { ...data, teams: [team] }, ask() {} }, () => Promise.resolve(center));
  await host.flush();
  button(host, "球员阵容").props.onClick();
  await host.flush();
  button(host, "了解球员").props.onClick();
  await host.flush();
  const detail = nodes(host.tree, node => node.props?.className === "player-detail")[0];
  assert.match(text(detail), /Jane Doe/);
  assert.match(text(detail), /CC BY-SA 4.0/);
  assert.match(text(detail), /人物肖像权可能另有限制/);
  assert.ok(nodes(detail, node => node.type === "a" && node.props.href === player.photo_file_page).length);
  host.unmount();
});

test("player detail explains why an unverified photo was omitted", async () => {
  const player = { id: "player", name: "测试球员", position: "中场", position_group: "midfielder",
    photo_reason: "页面记载的当前俱乐部与阵容不一致。" };
  const center = { team, roster: [player], matches: [], stats: {}, sources: [] };
  const host = component("v7TeamCenter.tsx", "TeamCenter", { team, data: { ...data, teams: [team] }, ask() {} }, () => Promise.resolve(center));
  await host.flush();
  button(host, "球员阵容").props.onClick();
  await host.flush();
  button(host, "了解球员").props.onClick();
  await host.flush();
  const detail = nodes(host.tree, node => node.props?.className === "player-detail")[0];
  assert.match(text(detail), /当前俱乐部与阵容不一致/);
  host.unmount();
});

test("team center shows every league standing by source rank and identifies the current club", async () => {
  const standings = [
    { rank: 3, team_id: "other", team_name: "另一支球队", played: 0, points: 0 },
    { rank: 2, team_id: team.id, team_name: team.name, played: 4, won: 2, drawn: 1, lost: 1, goals_for: 7, goals_against: 3, goal_difference: 4, points: 7 },
    { rank: 1, team_id: "leader", team_name: "榜首球队", played: 4, points: 10 },
  ];
  const center = { team, roster: [], matches: [], stats: {}, standings };
  const host = component("v7TeamCenter.tsx", "TeamCenter", { team, data: { ...data, teams: [team] }, ask() {} }, () => Promise.resolve(center));
  await host.flush();
  const table = nodes(host.tree, node => node.type === "table")[0];
  assert.ok(table, "league standings render as an accessible table");
  const body = nodes(table, node => node.type === "tbody")[0];
  const rows = nodes(body, node => node.type === "tr");
  assert.equal(rows.length, 3, "keep every club, including clubs the reader does not follow");
  assert.deepEqual(rows.map(row => text(nodes(row, node => node.type === "td")[0])), ["1", "2", "3"]);
  assert.deepEqual(standings.map(row => row.rank), [3, 2, 1], "sorting does not mutate the loaded snapshot");
  assert.equal(rows[1].props["aria-current"], true);
  assert.equal(rows.filter(row => row.props["aria-current"]).length, 1);
  assert.match(text(rows[1]), /阿森纳当前球队/);
  assert.ok(rows[1].props.className.includes("is-current"));
  const zeroCells = nodes(rows[2], node => node.type === "td").map(text);
  assert.ok(zeroCells.includes("0"), "zero is not treated as missing data");
  assert.ok(zeroCells.includes("—"), "missing source values stay unknown");
  host.unmount();
});

test("team center keeps standings hidden until results are explicitly revealed", async () => {
  for (const flag of ["hidden", "standings_hidden"]) {
    const paths = [];
    const host = component("v7TeamCenter.tsx", "TeamCenter", { team, data: { ...data, teams: [team] }, ask() {} }, path => {
      paths.push(path);
      const hidden = !path.includes("reveal=true");
      return Promise.resolve({ team, roster: [], matches: [], stats: { hidden: flag === "hidden" && hidden },
        standings_hidden: flag === "standings_hidden" && hidden,
        standings: [{ rank: 1, team_id: "other", team_name: "隐藏榜首球队", points: 12 }] });
    });
    await host.flush();
    assert.equal(nodes(host.tree, node => node.type === "table").length, 0);
    assert.doesNotMatch(text(host.tree), /隐藏榜首球队/);
    assert.ok(button(host, "显示比赛结果"));
    button(host, "显示比赛结果").props.onClick();
    await host.flush();
    assert.match(text(host.tree), /隐藏榜首球队/);
    assert.equal(paths.at(-1), "/teams/arsenal/center?reveal=true");
    host.unmount();
  }
});

test("team center schedule names each competition alongside its opponent", async () => {
  const center = { team, roster: [], stats: {}, matches: [
    { id: "euro", opponent: "欧洲对手", status: "scheduled", competition: "欧冠", competition_id: "CL", kickoff: "2026-09-30T19:00:00Z" },
    { id: "league", opponent: "联赛对手", status: "scheduled", competition: "英超", kickoff: "2026-10-03T14:00:00Z" },
  ] };
  const host = component("v7TeamCenter.tsx", "TeamCenter", { team, data: { ...data, teams: [team] }, ask() {} }, () => Promise.resolve(center));
  await host.flush();
  button(host, "比赛日程").props.onClick();
  await host.flush();
  const rows = nodes(host.tree, node => node.props?.className === "match-row");
  assert.equal(rows.length, 2);
  assert.match(text(rows[0]), /欧洲对手欧冠/);
  assert.match(text(rows[1]), /联赛对手英超/);
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
