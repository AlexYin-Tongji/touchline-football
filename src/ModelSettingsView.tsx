import { memo, useCallback, useEffect, useMemo, useState } from "react";
import {
  Check,
  CheckCircle2,
  ChevronRight,
  Database,
  Eye,
  EyeOff,
  Globe,
  Info,
  LoaderCircle,
  Plug,
  Plus,
  RefreshCw,
  Save,
  Search,
  Server,
  Shield,
  Trash2,
  TriangleAlert,
} from "lucide-react";
import { api, errorText, post, put } from "./api";
import { NetworkVerdictNotice, Spinner } from "./ui";
import type { NetworkDiagnostics } from "./types";

/**
 * One model, four sections.
 *
 * The page used to be three tabs whose middle tab assigned a model to each of six
 * functions. Two of those functions — photo planning and the review loop — could
 * not be inherited by design, and the rest could, which is how the active model
 * and the working model drifted apart without anybody seeing it. There is one
 * model now. What remains is the provider that serves it, the wire parameters
 * that belong to the model rather than the provider, and two unattended passes
 * that stay off until switched on.
 */

interface ModelEntry {
  id: string;
  name: string;
  max_output_tokens?: number | null;
  context_window?: number | null;
  temperature?: number | null;
  send_temperature?: boolean;
  reasoning_effort?: string | null;
  thinking_enabled?: boolean | null;
  timeout_seconds?: number | null;
}
interface Provider {
  id: string;
  name: string;
  base_url: string;
  protocol: "chat_completions" | "responses" | "anthropic_messages";
  api_key_set: boolean;
  models: ModelEntry[];
}
interface ProviderState {
  providers: Provider[];
  active: { provider_id: string; model: string } | null;
  background?: { photos: boolean; review: boolean };
  context?: ServerContext;
}
interface ServerContext {
  version: string;
  build_id: string;
  port: number;
  data_dir: string;
  database: { path: string; exists: boolean; bytes: number; key_present: boolean };
}
interface DataSource {
  id: string;
  name: string;
  configured: boolean;
  requires_key: boolean;
  optional_key?: boolean;
  capabilities: string[];
  quota?: { limit: number; used: number; remaining: number; resets_at?: string | null };
}
interface TestCheck {
  stage: string;
  ok: boolean;
  message: string;
}
interface TestResult {
  ok: boolean;
  usable?: boolean;
  message: string;
  checks?: TestCheck[];
  tool_support?: boolean | null;
  elapsed_ms?: number;
}
type Section = "model" | "api" | "server" | "sources";

const emptyProvider = (): Provider => ({
  id: "",
  name: "",
  base_url: "",
  protocol: "chat_completions",
  api_key_set: false,
  models: [],
});
const presets = [
  { name: "DeepSeek", url: "https://api.deepseek.com/v1", protocol: "chat_completions" as const },
  { name: "OpenAI", url: "https://api.openai.com/v1", protocol: "responses" as const },
  { name: "Anthropic", url: "https://api.anthropic.com", protocol: "anthropic_messages" as const },
];
const effortLevels = [
  { id: "", label: "不发送" },
  { id: "minimal", label: "minimal · 最省" },
  { id: "low", label: "low · 省" },
  { id: "medium", label: "medium · 中" },
  { id: "high", label: "high · 高" },
  { id: "xhigh", label: "xhigh · 很高" },
  { id: "max", label: "max · 最高" },
];
/** `256K` and `1M` are what a model card actually says; typing digits invites
 *  an order-of-magnitude slip. Decimal, like the DeepSeek Harness form. */
export const parseCount = (text: string): number | null => {
  const value = text.trim().toLowerCase().replace(/[,\s]/g, "");
  if (!value) return null;
  const match = value.match(/^(\d+(?:\.\d+)?)([km])?$/);
  if (!match) return null;
  const scale = match[2] === "m" ? 1_000_000 : match[2] === "k" ? 1_000 : 1;
  const number = Math.round(parseFloat(match[1]) * scale);
  return Number.isFinite(number) && number > 0 ? number : null;
};
export const formatCount = (value?: number | null): string => {
  if (!value) return "";
  if (value >= 1_000_000 && value % 100_000 === 0) return `${value / 1_000_000}M`;
  if (value >= 1_000 && value % 1_000 === 0) return `${value / 1_000}K`;
  return String(value);
};

const modelLabel = (model: ModelEntry) => model.name || model.id;
const providerOf = (providers: Provider[], id?: string | null) =>
  providers.find((item) => item.id === id) || null;

export const ModelSettings = memo(function ModelSettings({
  onChanged,
  notify,
}: {
  onChanged: () => Promise<void>;
  notify?: (message: string) => void;
}) {
  const [state, setState] = useState<ProviderState | null>(null);
  const [section, setSection] = useState<Section>("model");
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState("");
  const say = useCallback(
    (text: string) => {
      setMessage(text);
      notify?.(text);
    },
    [notify],
  );

  useEffect(() => {
    let valid = true;
    api<ProviderState>("/model-providers")
      .then((result) => valid && setState(result))
      .catch((err) => valid && setError(errorText(err)));
    return () => {
      valid = false;
    };
  }, []);

  const run = useCallback(
    async (name: string, fn: () => Promise<void>) => {
      if (busy) return;
      setBusy(name);
      setError("");
      setMessage("");
      try {
        await fn();
      } catch (err) {
        setError(errorText(err));
      } finally {
        setBusy("");
      }
    },
    [busy],
  );

  const reload = useCallback(async () => {
    const result = await api<ProviderState>("/model-providers");
    setState(result);
    return result;
  }, []);

  const activate = (provider: Provider, model: ModelEntry) =>
    run(`activate-${model.id}`, async () => {
      const result = await post<ProviderState>("/models/select", {
        provider_id: provider.id,
        model: model.id,
      });
      setState(result);
      await onChanged();
      say(`已使用 ${modelLabel(model)}，所有工作都走这个模型。`);
    });

  const active = state?.active;
  const activeProvider = providerOf(state?.providers || [], active?.provider_id);
  const activeModel = activeProvider?.models.find((item) => item.id === active?.model);

  return (
    <div className="model-settings">
      <div className="settings-head">
        <div>
          <h2>{activeModel ? modelLabel(activeModel) : "尚未选择模型"}</h2>
          <p>
            {activeProvider
              ? `${activeProvider.name} · ${activeProvider.base_url}`
              : "还没有连接模型服务商。"}
          </p>
        </div>
        {activeModel && activeProvider && (
          <span className="settings-state" data-ok={activeProvider.api_key_set ? "true" : "false"}>
            <CheckCircle2 size={15} />
            {activeProvider.api_key_set ? "密钥已保存" : "无密钥"}
          </span>
        )}
      </div>

      <nav className="settings-tabs" aria-label="配置分类">
        {(
          [
            { id: "model" as const, label: "模型", short: "模型", icon: Server },
            { id: "api" as const, label: "参数", short: "参数", icon: Plug },
            { id: "server" as const, label: "运行", short: "运行", icon: Database },
            { id: "sources" as const, label: "数据源", short: "数据", icon: Globe },
          ] as const
        ).map((item) => (
          <button
            key={item.id}
            className={section === item.id ? "active" : ""}
            aria-pressed={section === item.id}
            aria-label={item.label}
            title={item.label}
            data-short={item.short}
            onClick={() => setSection(item.id)}
          >
            <item.icon size={17} />
            <span>{item.label}</span>
          </button>
        ))}
      </nav>

      {error && (
        <div className="notice error" role="alert">
          {error}
        </div>
      )}
      {message && (
        <div className="notice success" role="status">
          <CheckCircle2 size={16} />
          {message}
        </div>
      )}

      {!state && !error ? (
        <Spinner text="正在读取配置" />
      ) : (
        <>
          {section === "model" && state && (
            <ModelSection
              state={state}
              busy={busy}
              activate={activate}
              onChanged={onChanged}
              reload={reload}
              say={say}
              run={run}
              setError={setError}
            />
          )}
          {section === "api" && state && activeProvider && activeModel && (
            <ApiSection
              provider={activeProvider}
              model={activeModel}
              busy={busy}
              run={run}
              reload={reload}
              onChanged={onChanged}
              say={say}
            />
          )}
          {section === "api" && state && (!activeProvider || !activeModel) && (
            <div className="empty-state compact">
              <Plug size={28} />
              <h3>先选一个模型</h3>
              <p>API 参数属于具体模型，选定模型后在这里调整。</p>
              <button className="button" onClick={() => setSection("model")}>
                去选模型
              </button>
            </div>
          )}
          {section === "server" && state && (
            <ServerSection context={state.context} state={state} busy={busy} run={run} reload={reload} say={say} />
          )}
          {section === "sources" && <SourceSection busy={busy} run={run} say={say} />}
        </>
      )}
    </div>
  );
});

/* ------------------------------------------------------------------ model */

function ModelSection({
  state,
  busy,
  activate,
  onChanged,
  reload,
  say,
  run,
  setError,
}: {
  state: ProviderState;
  busy: string;
  activate: (provider: Provider, model: ModelEntry) => void;
  onChanged: () => Promise<void>;
  reload: () => Promise<ProviderState>;
  say: (text: string) => void;
  run: (name: string, fn: () => Promise<void>) => Promise<void>;
  setError: (text: string) => void;
}) {
  const [editing, setEditing] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const activeId = state.active?.provider_id;
  const toggle = (id: string) =>
    setExpanded((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  return (
    <>
      <section className="settings-panel">
        <div className="settings-panel-head">
          <div>
            <h3>选择模型</h3>
            <p>所有功能共用这一个模型。点击即生效，不需要保存。</p>
          </div>
        </div>
        {!state.providers.length && (
          <p className="settings-hint">
            还没有服务商。在下面添加一个，填入接口地址和密钥后就能选择模型。
          </p>
        )}
        <div className="model-choice-list">
          {state.providers.map((provider) => (
            <div key={provider.id} className={`model-choice ${activeId === provider.id ? "current" : ""}`}>
              <div className="model-choice-head">
                <button className="model-choice-pick" onClick={() => toggle(provider.id)}>
                  <span className="provider-mark">{provider.name.slice(0, 1)}</span>
                  <span className="model-choice-title">
                    <strong>{provider.name}</strong>
                    <small>
                      {provider.base_url}
                      {provider.api_key_set ? "" : " · 无密钥"}
                    </small>
                  </span>
                  <span className="model-choice-current">
                    {activeId === provider.id && state.active?.model
                      ? modelLabel(provider.models.find((m) => m.id === state.active?.model) || { id: state.active.model, name: "" })
                      : `${provider.models.length} 个模型`}
                  </span>
                  <ChevronRight size={16} className={expanded.has(provider.id) ? "open" : ""} />
                </button>
                <button
                  className={`text-button model-choice-edit ${editing === provider.id ? "active" : ""}`}
                  onClick={() => setEditing(editing === provider.id ? null : provider.id)}
                >
                  {editing === provider.id ? "完成" : "编辑"}
                </button>
              </div>
              {(expanded.has(provider.id) || editing === provider.id) && (
              <div className="model-choice-body">
                {provider.models.map((model) => {
                  const chosen = activeId === provider.id && state.active?.model === model.id;
                  return (
                    <button
                      key={model.id}
                      className={`model-row ${chosen ? "chosen" : ""}`}
                      disabled={!!busy}
                      onClick={() => activate(provider, model)}
                    >
                      <span className="radio" aria-hidden="true">{chosen && <Check size={12} />}</span>
                      <span className="model-row-name">{modelLabel(model)}</span>
                      <span className="model-row-meta">
                        {model.context_window ? `${formatCount(model.context_window)} 窗口` : ""}
                        {model.context_window && model.max_output_tokens ? " · " : ""}
                        {model.max_output_tokens ? `${formatCount(model.max_output_tokens)} 输出` : ""}
                        {model.reasoning_effort ? ` · 推理 ${model.reasoning_effort}` : ""}
                      </span>
                    </button>
                  );
                })}
                {!provider.models.length && <p className="settings-hint">这个服务商还没有模型。</p>}
              </div>
              )}
              {editing === provider.id && (
                <ProviderEditor
                  provider={provider}
                  activeModelId={activeId === provider.id ? state.active?.model || null : null}
                  busy={busy}
                  run={run}
                  reload={reload}
                  onChanged={onChanged}
                  say={say}
                  setError={setError}
                  onDone={() => setEditing(null)}
                />
              )}
            </div>
          ))}
        </div>
        <ProviderCreator busy={busy} run={run} reload={reload} onChanged={onChanged} say={say} setError={setError} />
      </section>

      <section className="settings-panel">
        <div className="settings-panel-head">
          <div>
            <h3>后台自动整理</h3>
            <p>这两项会在你打开球队页时自动调用模型。默认关闭，打开后每次刷新都会产生调用。</p>
          </div>
        </div>
        {(["photos", "review"] as const).map((kind) => {
          const on = state.background?.[kind] === true;
          return (
            <label className="switch-row" key={kind}>
              <span>
                <strong>{kind === "photos" ? "球员照片排序" : "资料复核"}</strong>
                <small>
                  {kind === "photos"
                    ? "决定先核对哪些缺图球员。关闭时按原有顺序推进，不额外花费。"
                    : "决定下一轮重试谁。关闭时不复核，缺口保持原样。"}
                </small>
              </span>
              <button
                role="switch"
                aria-checked={on}
                className={`switch ${on ? "on" : ""}`}
                disabled={!!busy}
                onClick={() =>
                  run(`background-${kind}`, async () => {
                    const result = await put<ProviderState>("/models/background", {
                      photos: kind === "photos" ? !on : state.background?.photos === true,
                      review: kind === "review" ? !on : state.background?.review === true,
                    });
                    state.background = result.background;
                    say(on ? "已关闭，不再自动调用。" : "已开启，下次刷新会使用这个模型。");
                  })
                }
              >
                <span />
              </button>
            </label>
          );
        })}
      </section>
    </>
  );
}

function ProviderEditor({
  provider,
  activeModelId,
  busy,
  run,
  reload,
  onChanged,
  say,
  setError,
  onDone,
}: {
  provider: Provider;
  activeModelId: string | null;
  busy: string;
  run: (name: string, fn: () => Promise<void>) => Promise<void>;
  reload: () => Promise<ProviderState>;
  onChanged: () => Promise<void>;
  say: (text: string) => void;
  setError: (text: string) => void;
  onDone: () => void;
}) {
  const [draft, setDraft] = useState<Provider>(provider);
  const [key, setKey] = useState("");
  const [visible, setVisible] = useState(false);
  const [models, setModels] = useState<ModelEntry[]>(provider.models);
  const [query, setQuery] = useState("");
  const [manual, setManual] = useState("");
  const [confirm, setConfirm] = useState(false);
  useEffect(() => {
    setDraft(provider);
    setModels(provider.models);
    setKey("");
    setVisible(false);
    setConfirm(false);
  }, [provider]);
  const dirty =
    draft.name !== provider.name ||
    draft.base_url !== provider.base_url ||
    draft.protocol !== provider.protocol ||
    !!key ||
    JSON.stringify(models) !== JSON.stringify(provider.models);
  const savedActive = models.find((item) => item.id === activeModelId) || models[0];

  const save = () =>
    run(`save-${provider.id}`, async () => {
      await put(`/model-providers/${encodeURIComponent(provider.id)}`, {
        id: provider.id,
        name: draft.name.trim(),
        base_url: draft.base_url.trim(),
        protocol: draft.protocol,
        ...(key ? { api_key: key } : {}),
        models,
      });
      await reload();
      await onChanged();
      setKey("");
      say("已保存。");
    });

  const test = () =>
    run(`test-${provider.id}`, async () => {
      const chosen = savedActive || models[0];
      if (!chosen) {
        setError("请先填写至少一个模型。");
        return;
      }
      const result = await post<TestResult>("/models/test", {
        provider_id: provider.id,
        base_url: draft.base_url.trim(),
        protocol: draft.protocol,
        model: chosen.id,
        ...(key ? { api_key: key } : {}),
        models,
      });
      if (result.usable ?? result.ok) say(result.message || "连接正常。");
      else setError(result.message || "连接未成功，请检查配置。");
    });

  const discoverModels = () =>
    run(`discover-${provider.id}`, async () => {
      const result = await post<{ models: ModelEntry[] }>("/models/discover", {
        provider_id: provider.id,
        base_url: draft.base_url,
        protocol: draft.protocol,
        ...(key ? { api_key: key } : {}),
      });
      const merged = [
        ...models,
        ...result.models.filter((item) => !models.some((old) => old.id === item.id)),
      ];
      setModels(merged);
      say(
        result.models.length
          ? `找到 ${result.models.length} 个模型，勾选要保留的。`
          : "服务商没有返回模型列表，可以手动添加模型 ID。",
      );
    });

  const needle = query.trim().toLowerCase();
  const filtered = useMemo(
    () => models.filter((item) => `${item.id} ${item.name}`.toLowerCase().includes(needle)),
    [models, needle],
  );

  return (
    <div className="provider-editor">
      <div className="field-grid">
        <label className="field">
          <span>显示名称</span>
          <input value={draft.name} onChange={(event) => setDraft({ ...draft, name: event.target.value })} />
        </label>
        <label className="field">
          <span>API 协议</span>
          <select
            value={draft.protocol}
            onChange={(event) => setDraft({ ...draft, protocol: event.target.value as Provider["protocol"] })}
          >
            <option value="chat_completions">OpenAI Chat Completions</option>
            <option value="responses">OpenAI Responses</option>
            <option value="anthropic_messages">Anthropic Messages</option>
          </select>
        </label>
        <label className="field wide">
          <span>接口地址</span>
          <input
            type="url"
            value={draft.base_url}
            placeholder={
              draft.protocol === "anthropic_messages"
                ? "https://api.anthropic.com"
                : "https://api.example.com/v1"
            }
            onChange={(event) => setDraft({ ...draft, base_url: event.target.value })}
          />
          <small>
            {draft.protocol === "anthropic_messages"
              ? "只填到域名，不要加 /v1。系统会自动补 /v1/messages。"
              : "填到 /v1 为止，不要写具体的接口路径。"}
          </small>
        </label>
        <label className="field wide">
          <span>API 密钥</span>
          <div className="password-field">
            <input
              type={visible ? "text" : "password"}
              autoComplete="new-password"
              value={key}
              onChange={(event) => setKey(event.target.value)}
              placeholder={provider.api_key_set ? "已保存，留空则保持不变" : "粘贴服务商提供的密钥"}
            />
            <button type="button" className="icon-button" aria-label={visible ? "隐藏密钥" : "显示密钥"} onClick={() => setVisible(!visible)}>
              {visible ? <EyeOff size={16} /> : <Eye size={16} />}
            </button>
          </div>
        </label>
      </div>

      <div className="model-editor-head">
        <div>
          <h4>模型</h4>
          <p>每个模型有自己的窗口与输出上限，设置后随模型一起保存。</p>
        </div>
        <div className="model-editor-actions">
          <button className="button" disabled={!draft.base_url || !!busy} onClick={discoverModels}>
            {busy.startsWith("discover") ? <LoaderCircle size={15} className="spin" /> : <RefreshCw size={15} />}
            获取模型
          </button>
          <button
            className="button"
            disabled={!!busy}
            onClick={() => {
              const id = `model_${models.length + 1}`;
              setModels([...models, { id, name: "" }]);
            }}
          >
            <Plus size={15} />
            手动添加
          </button>
        </div>
      </div>

      {models.length > 6 && (
        <label className="search-input compact">
          <Search size={15} />
          <input aria-label="搜索模型" placeholder="搜索模型" value={query} onChange={(event) => setQuery(event.target.value)} />
        </label>
      )}
      <div className="model-editor-list">
        {filtered.map((model) => (
          <ModelRow
            key={model.id}
            model={model}
            busy={!!busy}
            active={provider.models.some((item) => item.id === model.id) && activeModelId === model.id}
            onChange={(next) => setModels(models.map((item) => (item === model ? next : item)))}
            onRemove={() => setModels(models.filter((item) => item !== model))}
          />
        ))}
        {!filtered.length && <p className="settings-hint">没有模型。点“获取模型”或手动添加。</p>}
      </div>

      <div className="provider-actions">
        <button className="button primary" disabled={!!busy || !dirty} onClick={save}>
          {busy.startsWith("save") ? <LoaderCircle size={15} className="spin" /> : <Save size={15} />}
          保存
        </button>
        <button className="button" disabled={!!busy} onClick={test}>
          {busy.startsWith("test") ? <LoaderCircle size={15} className="spin" /> : <Plug size={15} />}
          检测连接
        </button>
        <button className="text-button" disabled={!!busy} onClick={onDone}>
          收起
        </button>
        <button
          className={`text-button danger ${confirm ? "armed" : ""}`}
          disabled={!!busy}
          onClick={() => {
            if (!confirm) {
              setConfirm(true);
              window.setTimeout(() => setConfirm(false), 4000);
              return;
            }
            void run(`delete-${provider.id}`, async () => {
              await api(`/model-providers/${encodeURIComponent(provider.id)}`, { method: "DELETE" });
              await reload();
              await onChanged();
              say("服务商已移除。");
              onDone();
            });
          }}
        >
          <Trash2 size={14} />
          {confirm ? "再次点击确认移除" : "移除此服务商"}
        </button>
      </div>
    </div>
  );
}

function ModelRow({
  model,
  busy,
  active,
  onChange,
  onRemove,
}: {
  model: ModelEntry;
  busy: boolean;
  active: boolean;
  onChange: (model: ModelEntry) => void;
  onRemove: () => void;
}) {
  const [open, setOpen] = useState(false);
  return (
    <div className={`model-editor-row ${active ? "current" : ""}`}>
      <div className="model-editor-main">
        <input
          className="model-id"
          aria-label="模型 ID"
          value={model.id}
          disabled={busy}
          onChange={(event) => onChange({ ...model, id: event.target.value })}
        />
        <input
          className="model-name"
          aria-label="显示名称"
          placeholder="显示名称（可留空）"
          value={model.name}
          disabled={busy}
          onChange={(event) => onChange({ ...model, name: event.target.value })}
        />
        <button className="text-button" onClick={() => setOpen(!open)} aria-expanded={open}>
          参数
          <ChevronRight size={14} className={open ? "open" : ""} />
        </button>
        <button className="icon-button" aria-label="删除模型" disabled={busy} onClick={onRemove}>
          <Trash2 size={14} />
        </button>
      </div>
      {open && (
        <div className="model-editor-params">
          <label className="field">
            <span>上下文窗口</span>
            <input
              inputMode="numeric"
              placeholder="256K"
              defaultValue={formatCount(model.context_window)}
              onBlur={(event) => {
                const value = event.target.value.trim();
                onChange({ ...model, context_window: value ? parseCount(value) : null });
              }}
            />
            <small>只用于提示，不会截断原文</small>
          </label>
          <label className="field">
            <span>最大输出</span>
            <input
              inputMode="numeric"
              placeholder="32K"
              defaultValue={formatCount(model.max_output_tokens)}
              onBlur={(event) => {
                const value = event.target.value.trim();
                onChange({ ...model, max_output_tokens: value ? parseCount(value) : null });
              }}
            />
            <small>写长稿被截断时调高</small>
          </label>
          <label className="field">
            <span>推理等级</span>
            <select
              value={model.reasoning_effort || ""}
              onChange={(event) => onChange({ ...model, reasoning_effort: event.target.value || null })}
            >
              {effortLevels.map((level) => (
                <option key={level.id} value={level.id}>
                  {level.label}
                </option>
              ))}
            </select>
            <small>三家协议写法不同，这里统一用等级</small>
          </label>
          <label className="field">
            <span>温度</span>
            <input
              inputMode="decimal"
              placeholder="不发送"
              defaultValue={model.temperature ?? ""}
              onBlur={(event) => {
                const value = event.target.value.trim();
                const number = value ? Number(value) : null;
                onChange({
                  ...model,
                  temperature: number !== null && Number.isFinite(number) ? number : null,
                  send_temperature: number !== null && Number.isFinite(number),
                });
              }}
            />
            <small>留空则请求里不带这个参数</small>
          </label>
        </div>
      )}
    </div>
  );
}

function ProviderCreator({
  busy,
  run,
  reload,
  onChanged,
  say,
  setError,
}: {
  busy: string;
  run: (name: string, fn: () => Promise<void>) => Promise<void>;
  reload: () => Promise<ProviderState>;
  onChanged: () => Promise<void>;
  say: (text: string) => void;
  setError: (text: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState<Provider>(emptyProvider);
  const [key, setKey] = useState("");
  const [visible, setVisible] = useState(false);
  const [models, setModels] = useState<ModelEntry[]>([]);
  const [manual, setManual] = useState("");

  const create = () =>
    run("create", async () => {
      await post<ProviderState & { saved_provider_id: string }>("/model-providers", {
        name: draft.name.trim(),
        base_url: draft.base_url.trim(),
        protocol: draft.protocol,
        ...(key ? { api_key: key } : {}),
        models,
      });
      await reload();
      await onChanged();
      setOpen(false);
      setDraft(emptyProvider());
      setKey("");
      setModels([]);
      say("服务商已添加，选择要使用的模型。");
    });

  if (!open)
    return (
      <button className="button add-provider" onClick={() => setOpen(true)}>
        <Plus size={16} />
        添加服务商
      </button>
    );

  return (
    <div className="provider-creator">
      <div className="presets">
        {presets.map((preset) => (
          <button
            key={preset.name}
            className={draft.base_url === preset.url ? "active" : ""}
            onClick={() => setDraft({ ...emptyProvider(), name: preset.name, base_url: preset.url, protocol: preset.protocol })}
          >
            {preset.name}
          </button>
        ))}
        <button onClick={() => setDraft(emptyProvider())}>自定义</button>
      </div>
      <div className="field-grid">
        <label className="field">
          <span>显示名称</span>
          <input value={draft.name} placeholder="例如：我的 DeepSeek" onChange={(event) => setDraft({ ...draft, name: event.target.value })} />
        </label>
        <label className="field">
          <span>API 协议</span>
          <select
            value={draft.protocol}
            onChange={(event) => setDraft({ ...draft, protocol: event.target.value as Provider["protocol"] })}
          >
            <option value="chat_completions">OpenAI Chat Completions</option>
            <option value="responses">OpenAI Responses</option>
            <option value="anthropic_messages">Anthropic Messages</option>
          </select>
        </label>
        <label className="field wide">
          <span>接口地址</span>
          <input
            type="url"
            value={draft.base_url}
            placeholder={draft.protocol === "anthropic_messages" ? "https://api.anthropic.com" : "https://api.example.com/v1"}
            onChange={(event) => setDraft({ ...draft, base_url: event.target.value })}
          />
        </label>
        <label className="field wide">
          <span>API 密钥</span>
          <div className="password-field">
            <input
              type={visible ? "text" : "password"}
              autoComplete="new-password"
              value={key}
              onChange={(event) => setKey(event.target.value)}
              placeholder="粘贴服务商提供的密钥"
            />
            <button type="button" className="icon-button" aria-label={visible ? "隐藏密钥" : "显示密钥"} onClick={() => setVisible(!visible)}>
              {visible ? <EyeOff size={16} /> : <Eye size={16} />}
            </button>
          </div>
        </label>
      </div>
      <div className="manual-model">
        <input
          value={manual}
          placeholder="模型 ID，例如 claude-sonnet-5-5"
          onChange={(event) => setManual(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter") event.preventDefault();
          }}
        />
        <button
          className="button"
          disabled={!manual.trim()}
          onClick={() => {
            const id = manual.trim();
            if (!models.some((item) => item.id === id)) setModels([...models, { id, name: "" }]);
            setManual("");
          }}
        >
          <Plus size={15} />
          添加
        </button>
      </div>
      {!!models.length && (
        <div className="model-editor-list">
          {models.map((model) => (
            <ModelRow
              key={model.id}
              model={model}
              busy={false}
              active={false}
              onChange={(next) => setModels(models.map((item) => (item === model ? next : item)))}
              onRemove={() => setModels(models.filter((item) => item !== model))}
            />
          ))}
        </div>
      )}
      <div className="provider-actions">
        <button className="button primary" disabled={!!busy || !draft.name.trim() || !draft.base_url || !models.length} onClick={create}>
          {busy === "create" ? <LoaderCircle size={15} className="spin" /> : <Check size={15} />}
          添加
        </button>
        <button
          className="button"
          disabled={!!busy || !draft.base_url || !models.length}
          onClick={() =>
            run("test-new", async () => {
              const result = await post<TestResult>("/models/test", {
                base_url: draft.base_url.trim(),
                protocol: draft.protocol,
                model: models[0].id,
                ...(key ? { api_key: key } : {}),
                models,
              });
              if (result.usable ?? result.ok) say(result.message || "连接正常。");
              else setError(result.message || "连接未成功，请检查配置。");
            })
          }
        >
          {busy === "test-new" ? <LoaderCircle size={15} className="spin" /> : <Plug size={15} />}
          检测连接
        </button>
        <button className="text-button" onClick={() => setOpen(false)}>
          取消
        </button>
      </div>
    </div>
  );
}

/* -------------------------------------------------------------- api params */

function ApiSection({
  provider,
  model,
  busy,
  run,
  reload,
  onChanged,
  say,
}: {
  provider: Provider;
  model: ModelEntry;
  busy: string;
  run: (name: string, fn: () => Promise<void>) => Promise<void>;
  reload: () => Promise<ProviderState>;
  onChanged: () => Promise<void>;
  say: (text: string) => void;
}) {
  const [draft, setDraft] = useState(model);
  const [window_, setWindow] = useState(formatCount(model.context_window));
  const [output, setOutput] = useState(formatCount(model.max_output_tokens));
  const [temperature, setTemperature] = useState(model.temperature ?? "");
  const [timeout_, setTimeout_] = useState(String(model.timeout_seconds ?? ""));
  useEffect(() => {
    setDraft(model);
    setWindow(formatCount(model.context_window));
    setOutput(formatCount(model.max_output_tokens));
    setTemperature(model.temperature ?? "");
    setTimeout_(String(model.timeout_seconds ?? ""));
  }, [model]);
  const save = () =>
    run("api-save", async () => {
      const next: ModelEntry = {
        ...draft,
        context_window: parseCount(window_),
        max_output_tokens: parseCount(output),
        temperature: temperature === "" ? null : Number(temperature),
        send_temperature: temperature !== "" && Number.isFinite(Number(temperature)),
        timeout_seconds: timeout_ === "" ? null : Number(timeout_),
      };
      await put(`/model-providers/${encodeURIComponent(provider.id)}`, {
        id: provider.id,
        name: provider.name,
        base_url: provider.base_url,
        protocol: provider.protocol,
        models: provider.models.map((item) => (item.id === model.id ? next : item)),
      });
      await reload();
      await onChanged();
      say("参数已保存，下一次请求生效。");
    });
  return (
    <>
      <section className="settings-panel">
        <div className="settings-panel-head">
          <div>
            <h3>{modelLabel(model)} 的请求参数</h3>
            <p>
              {provider.name} · {provider.protocol === "anthropic_messages" ? "Anthropic Messages" : provider.protocol === "responses" ? "OpenAI Responses" : "OpenAI Chat Completions"}
            </p>
          </div>
        </div>
        <div className="field-grid">
          <label className="field">
            <span>上下文窗口</span>
            <input inputMode="numeric" placeholder="256K" value={window_} onChange={(event) => setWindow(event.target.value)} />
            <small>输入上限。仅用于提示，不会自动截断原文。</small>
          </label>
          <label className="field">
            <span>最大输出</span>
            <input inputMode="numeric" placeholder="32K" value={output} onChange={(event) => setOutput(event.target.value)} />
            <small>长稿被中途截断时调高这里。</small>
          </label>
          <label className="field">
            <span>推理等级</span>
            <select
              value={draft.reasoning_effort || ""}
              onChange={(event) => setDraft({ ...draft, reasoning_effort: event.target.value || null })}
            >
              {effortLevels.map((level) => (
                <option key={level.id} value={level.id}>
                  {level.label}
                </option>
              ))}
            </select>
            <small>
              {provider.protocol === "anthropic_messages"
                ? "换算成 thinking.budget_tokens。开启后不发温度。"
                : provider.protocol === "responses"
                  ? "作为 reasoning.effort 发送。"
                  : "作为 reasoning_effort 发送。部分模型会拒绝这个参数。"}
            </small>
          </label>
          <label className="field">
            <span>温度</span>
            <input inputMode="decimal" placeholder="不发送" value={temperature} onChange={(event) => setTemperature(event.target.value)} />
            <small>留空表示请求里不带温度参数。开启推理时不会发送。</small>
          </label>
          <label className="field">
            <span>请求超时（秒）</span>
            <input inputMode="numeric" placeholder="45" value={timeout_} onChange={(event) => setTimeout_(event.target.value)} />
            <small>长文整理卡住时适当调高，10–300。</small>
          </label>
        </div>
        <div className="provider-actions">
          <button className="button primary" disabled={!!busy} onClick={save}>
            {busy === "api-save" ? <LoaderCircle size={15} className="spin" /> : <Save size={15} />}
            保存参数
          </button>
        </div>
      </section>
      <p className="settings-footnote">
        <Info size={14} />
        参数按模型保存。同一个服务商下的另一个模型不会继承这里的设置。
      </p>
    </>
  );
}

/* ------------------------------------------------------------------ server */

function ServerSection({
  context,
  state,
  busy,
  run,
  reload,
  say,
}: {
  context?: ServerContext;
  state: ProviderState;
  busy: string;
  run: (name: string, fn: () => Promise<void>) => Promise<void>;
  reload: () => Promise<ProviderState>;
  say: (text: string) => void;
}) {
  return (
    <>
      <section className="settings-panel">
        <div className="settings-panel-head">
          <div>
            <h3>端口</h3>
            <p>当前监听在 127.0.0.1:{context?.port ?? 9080}，只在本人电脑上可访问。</p>
          </div>
        </div>
        <dl className="fact-list">
          <div>
            <dt>端口</dt>
            <dd>{context?.port ?? 9080}</dd>
          </div>
          <div>
            <dt>访问地址</dt>
            <dd>http://127.0.0.1:{context?.port ?? 9080}</dd>
          </div>
          <div>
            <dt>修改方式</dt>
            <dd>
              设置环境变量 <code>TOUCHLINE_PORT</code> 后重启。界面不提供直接修改：正在运行的进程已经绑定了旧端口，
              而旧进程仍会持有数据库，同时启动两个会同时写一个文件。
            </dd>
          </div>
        </dl>
        <div className="provider-actions">
          <button
            className="button"
            disabled={!!busy}
            onClick={() =>
              run("refresh-context", async () => {
                await reload();
                say("已重新读取服务状态。");
              })
            }
          >
            {busy === "refresh-context" ? <LoaderCircle size={15} className="spin" /> : <RefreshCw size={15} />}
            重新读取
          </button>
        </div>
      </section>
      <section className="settings-panel">
        <div className="settings-panel-head">
          <div>
            <h3>数据库</h3>
            <p>所有球队、来源、对话与密钥都在这一个文件里。</p>
          </div>
        </div>
        <dl className="fact-list">
          <div>
            <dt>数据目录</dt>
            <dd><code>{context?.data_dir || "data/"}</code></dd>
          </div>
          <div>
            <dt>数据库</dt>
            <dd>
              <code>{context?.database.path || "data/touchline.sqlite3"}</code>
              <small>
                {context?.database.exists ? ` · ${(context.database.bytes / 1024 / 1024).toFixed(1)} MB` : " · 尚未创建"}
              </small>
            </dd>
          </div>
          <div>
            <dt>加密密钥</dt>
            <dd>
              {context?.database.key_present ? (
                <>touchline.key 存在 · 与数据库是一个整体，移动或删除数据库时必须一起带走</>
              ) : (
                <span className="warn">
                  <TriangleAlert size={14} /> touchline.key 不存在，已保存的密钥将无法解密
                </span>
              )}
            </dd>
          </div>
          <div>
            <dt>版本</dt>
            <dd>
              {context?.version || "—"} <small>· {context?.build_id || ""}</small>
            </dd>
          </div>
        </dl>
      </section>
      <p className="settings-footnote">
        <Shield size={14} />
        这两项都是只读展示。改动需要停止服务后在文件层面完成。
      </p>
    </>
  );
}

/* ----------------------------------------------------------------- sources */

function SourceSection({
  busy,
  run,
  say,
}: {
  busy: string;
  run: (name: string, fn: () => Promise<void>) => Promise<void>;
  say: (text: string) => void;
}) {
  const [sources, setSources] = useState<DataSource[]>([]);
  const [keys, setKeys] = useState<Record<string, string>>({});
  const [sourceError, setSourceError] = useState<{ id: string; text: string } | null>(null);
  const [loaded, setLoaded] = useState(false);
  useEffect(() => {
    let valid = true;
    api<{ sources: DataSource[] }>("/data-sources")
      .then((result) => {
        if (!valid) return;
        setSources(result.sources.filter((item) => item.requires_key || item.optional_key));
        setLoaded(true);
      })
      .catch(() => valid && setLoaded(true));
    return () => {
      valid = false;
    };
  }, []);
  return (
    <>
      <section className="settings-panel">
        <div className="settings-panel-head">
          <div>
            <h3>数据来源密钥</h3>
            <p>密钥只存服务端加密库，不会回显，也不会写进日志或地址。留空保存表示清除。</p>
          </div>
        </div>
        {!loaded && <Spinner text="读取数据来源" />}
        {loaded && !sources.length && (
          <p className="settings-hint">当前没有需要密钥的数据来源。</p>
        )}
        <div className="source-list">
          {sources.map((source) => (
            <div className="source-row" key={source.id}>
              <div className="source-row-head">
                <div>
                  <strong>{source.name}</strong>
                  <small>
                  {source.configured ? "已配置密钥" : source.optional_key ? "未配置 · 使用免费公开档位" : "未配置"}
                    {source.quota ? ` · 今日 ${source.quota.used}/${source.quota.limit}` : ""}
                  </small>
                </div>
                <span className={`source-badge ${source.configured ? "on" : source.optional_key ? "free" : ""}`}>
                  {source.configured ? "启用" : source.optional_key ? "免费档" : "未启用"}
                </span>
              </div>
              <div className="source-row-form">
                <input
                  type="password"
                  autoComplete="new-password"
                  placeholder={source.configured ? "已保存，留空则清除" : "粘贴密钥"}
                  value={keys[source.id] || ""}
                  onChange={(event) => setKeys({ ...keys, [source.id]: event.target.value })}
                />
                <button
                  className="button"
                  disabled={!!busy}
                  onClick={() =>
                    run(`source-${source.id}`, async () => {
                      const value = keys[source.id] || "";
                      if (!value) {
                        await api(`/data-sources/${source.id}/credential`, { method: "DELETE" });
                        say(`${source.name} 的密钥已清除。`);
                      } else {
                        await put(`/data-sources/${source.id}/credential`, { api_key: value });
                        say(`${source.name} 的密钥已保存。`);
                      }
                      const result = await api<{ sources: DataSource[] }>("/data-sources");
                      setSources(result.sources.filter((item) => item.requires_key || item.optional_key));
                      setKeys({ ...keys, [source.id]: "" });
                    })
                  }
                >
                  {busy === `source-${source.id}` ? <LoaderCircle size={15} className="spin" /> : <Save size={15} />}
                  保存
                </button>
                <button
                  className="button"
                  disabled={!!busy || !source.configured}
                  onClick={() =>
                    run(`verify-${source.id}`, async () => {
                      const result = await post<{ ok: boolean; message: string }>(
                        `/data-sources/${source.id}/test`,
                      );
                      if (result.ok) say(result.message);
                      else setSourceError({ id: source.id, text: result.message });
                    })
                  }
                >
                  {busy === `verify-${source.id}` ? <LoaderCircle size={15} className="spin" /> : <Plug size={15} />}
                  检测
                </button>
              </div>
              {sourceError?.id === source.id && (
                <p className="source-verdict bad" role="alert">
                  {sourceError.text}
                </p>
              )}
            </div>
          ))}
        </div>
      </section>
      <p className="settings-footnote">
        <Info size={14} />
        付费档位是否真的返回数据要以实际查询为准：免费档会把结果替换成样例数据，因此配置成功后仍需核验一次球队身份。
      </p>
    </>
  );
}
