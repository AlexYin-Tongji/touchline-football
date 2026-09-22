import { useEffect, useState } from "react";
import {
  Check,
  CheckCircle2,
  ChevronRight,
  Download,
  Eye,
  EyeOff,
  LoaderCircle,
  MessageCircle,
  Newspaper,
  Plus,
  Search,
  Server,
  Shield,
  Trash2,
} from "lucide-react";
import { api, errorText, post, put } from "./api";
import { Spinner } from "./components";
import "./modelSettings.css";

interface Model {
  id: string;
  name: string;
  max_output_tokens?: number;
}
interface Provider {
  id: string;
  name: string;
  base_url: string;
  protocol: "chat_completions" | "responses" | "anthropic_messages";
  api_key_set: boolean;
  models: Model[];
}
interface ProviderState {
  providers: Provider[];
  active: { provider_id: string; model: string } | null;
  roles?: Partial<Record<ModelRole, ModelSelection | null>>;
  resolved_roles?: Partial<Record<ModelRole, ModelSelection | null>>;
}
type ModelSelection = { provider_id: string; model: string };
type ModelRole = "feed" | "chat" | "team" | "verify";
const roleDescriptions = [
  {
    id: "feed" as const,
    title: "球队动态",
    icon: Newspaper,
    text: "整理新闻、提炼中文消息",
    context: "读取当前球队的新闻来源，不读取私人对话。",
  },
  {
    id: "chat" as const,
    title: "足球对话",
    icon: MessageCircle,
    text: "回答提问、持续追问",
    context: "读取当前会话与球队资料；新会话有独立上下文。",
  },
  {
    id: "team" as const,
    title: "球队资料解读",
    icon: Shield,
    text: "解释球队与球员资料",
    context: "读取可追溯的球队资料；比分与阵容由数据源更新。",
  },
  {
    id: "verify" as const,
    title: "原文核验",
    icon: CheckCircle2,
    text: "核对证据与候选答复",
    context: "只核对本次已读证据和候选答复，不读取私人对话。",
  },
];
const empty = (): Provider => ({
  id: "",
  name: "",
  base_url: "",
  protocol: "chat_completions",
  api_key_set: false,
  models: [],
});
const presets = [
  {
    name: "DeepSeek",
    id: "deepseek",
    url: "https://api.deepseek.com/v1",
    protocol: "chat_completions" as const,
  },
  {
    name: "OpenAI",
    id: "openai",
    url: "https://api.openai.com/v1",
    protocol: "responses" as const,
  },
  {
    name: "Anthropic",
    id: "anthropic",
    url: "https://api.anthropic.com",
    protocol: "anthropic_messages" as const,
  },
];

export function ModelSettings({
  onChanged,
}: {
  onChanged: () => Promise<void>;
}) {
  const [state, setState] = useState<ProviderState | null>(null);
  const [draft, setDraft] = useState<Provider>(empty);
  const [editing, setEditing] = useState<string | null>(null);
  const [key, setKey] = useState("");
  const [visible, setVisible] = useState(false);
  const [models, setModels] = useState<Model[]>([]);
  const [query, setQuery] = useState("");
  const [manual, setManual] = useState("");
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [deleteConfirm, setDeleteConfirm] = useState(false);
  const [section, setSection] = useState<"roles" | "providers">("roles");
  const choose = (provider: Provider | null) => {
    setDraft(provider || empty());
    setEditing(provider?.id || null);
    setKey("");
    setVisible(false);
    setModels(provider?.models || []);
    setQuery("");
    setManual("");
    setError("");
    setMessage("");
    setDeleteConfirm(false);
  };
  const reload = async (selectId?: string) => {
    const result = await api<ProviderState>("/model-providers");
    setState(result);
    if (selectId)
      choose(result.providers.find((item) => item.id === selectId) || null);
    return result;
  };
  useEffect(() => {
    let valid = true;
    api<ProviderState>("/model-providers")
      .then((result) => {
        if (!valid) return;
        setState(result);
        choose(
          result.providers.find(
            (item) => item.id === result.active?.provider_id,
          ) ||
            result.providers[0] ||
            null,
        );
      })
      .catch((err) => valid && setError(errorText(err)));
    return () => {
      valid = false;
    };
  }, []);
  const action = async (name: string, fn: () => Promise<void>) => {
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
  };
  const save = () =>
    action("save", async () => {
      const body = {
        ...(editing ? { id: editing } : {}),
        name: draft.name.trim(),
        base_url: draft.base_url.trim(),
        protocol: draft.protocol,
        ...(key ? { api_key: key } : {}),
        models: draft.models,
      };
      let savedId = editing;
      if (editing)
        await put(`/model-providers/${encodeURIComponent(editing)}`, body);
      else {
        const result = await post<ProviderState & { saved_provider_id: string }>(
          "/model-providers",
          body,
        );
        savedId = result.saved_provider_id;
      }
      await reload(savedId || undefined);
      await onChanged();
      setMessage("服务商已保存。可在功能分工中为每项功能选择模型。");
    });
  const discover = () =>
    action("discover", async () => {
      const result = await post<{ models: Model[] }>("/models/discover", {
        provider_id: editing || undefined,
        base_url: draft.base_url,
        protocol: draft.protocol,
        ...(key ? { api_key: key } : {}),
      });
      setModels([
        ...draft.models,
        ...result.models.filter(
          (item) => !draft.models.some((selected) => selected.id === item.id),
        ),
      ]);
      setMessage(
        result.models.length
          ? `找到 ${result.models.length} 个模型，请选择要使用的模型。`
          : "服务商没有返回模型列表，可以手动添加模型 ID。",
      );
    });
  const activate = (model: Model) =>
    action(`select-${model.id}`, async () => {
      if (!editing) return;
      const result = await post<ProviderState>("/models/select", {
        provider_id: editing,
        model: model.id,
      });
      setState(result);
      await onChanged();
      setMessage(
        `默认模型已设为 ${model.name || model.id}。单独分配的功能模型保持不变。`,
      );
    });
  const filtered = models.filter((item) =>
    `${item.id} ${item.name}`.toLowerCase().includes(query.toLowerCase()),
  );
  const savedModels =
    state?.providers.find((provider) => provider.id === editing)?.models || [];
  return (
    <div className="model-settings">
      <div className="settings-intro">
        <p>为动态整理、足球对话、球队解读和原文核验，选择合适的模型。</p>
        {state && <span className="settings-inventory">{state.providers.length} 个服务商 · {state.providers.reduce((count, provider) => count + provider.models.length, 0)} 个可用模型</span>}
      </div>
      <div className="settings-tabs" aria-label="模型设置分类">
        <button
          className={section === "roles" ? "active" : ""}
          aria-pressed={section === "roles"}
          onClick={() => setSection("roles")}
        >
          <Shield size={18} />功能分工
        </button>
        <button
          className={section === "providers" ? "active" : ""}
          aria-pressed={section === "providers"}
          onClick={() => setSection("providers")}
        >
          <Server size={18} />服务商与模型
        </button>
      </div>
      {error && (
        <div className="notice error" role="alert">
          {error}
        </div>
      )}
      {message && (
        <div className="notice success" role="status">
          <CheckCircle2 size={17} />
          {message}
        </div>
      )}
      {!state && !error ? (
        <Spinner text="读取模型配置" />
      ) : (
        <>
          {section === "roles" && (
            <section className="model-role-settings">
              <div className="role-intro">
                <h3>安排模型分工</h3>
                <p>
                  共用一个默认模型，或为每项功能单独选择。修改用于新任务，已有对话保留原模型。
                </p>
                <p>不同工作可以并行处理，内容通过来源核验后再发布。</p>
              </div>
              <div className="model-role-grid">
                {roleDescriptions.map((role) => (
                  <article className="model-role-card" key={role.id}>
                    <span className="role-icon">
                      <role.icon size={21} />
                    </span>
                    <h4>{role.title}</h4>
                    <p>{role.text}</p>
                    <label htmlFor={`role-${role.id}`}>使用模型</label>
                    <select
                      id={`role-${role.id}`}
                      aria-label={`${role.title}使用模型`}
                      disabled={!!busy}
                      value={
                        state?.roles?.[role.id]
                          ? `${state.roles[role.id]!.provider_id}\t${state.roles[role.id]!.model}`
                          : ""
                      }
                      onChange={(event) => {
                        const [provider_id, model] =
                          event.target.value.split("\t");
                        void action(`role-${role.id}`, async () => {
                          const result = await put<ProviderState>(
                            "/models/roles",
                            {
                              roles: {
                                [role.id]: provider_id
                                  ? { provider_id, model }
                                  : null,
                              },
                            },
                          );
                          setState(result);
                          await onChanged();
                          setMessage(`${role.title}的模型已更新。`);
                        });
                      }}
                    >
                      <option value="">
                        {role.id === "verify" ? "跟随当前任务模型" : `跟随默认${state?.active ? ` · ${state.active.model}` : "（尚未设置）"}`}
                      </option>
                      {state?.providers.map((provider) => (
                        <optgroup key={provider.id} label={provider.name}>
                          {provider.models.map((model) => (
                            <option
                              key={model.id}
                              value={`${provider.id}\t${model.id}`}
                            >
                              {model.name || model.id}
                            </option>
                          ))}
                        </optgroup>
                      ))}
                    </select>
                    <small className="role-context">{role.context}</small>
                  </article>
                ))}
              </div>
              <div className="context-boundary">
                <Shield size={18} />
                <div>
                  <strong>你的对话有自己的上下文</strong>
                  <p>
                    动态中的“问问助手”会带入消息来源。个人对话独立保存，不会进入新闻整理；切换球队后，只查看该队的对话记录。
                  </p>
                </div>
              </div>
              <button
                className="button"
                onClick={() => setSection("providers")}
              >
                <Plus size={15} /> 管理服务商与可用模型{" "}
                <ChevronRight size={15} />
              </button>
            </section>
          )}
          {section === "providers" && (
            <div className="provider-layout">
              <aside className="provider-list">
                <h3 className="provider-list-heading">我的服务商</h3>
                {state?.providers.map((provider) => (
                  <button
                    key={provider.id}
                    disabled={!!busy}
                    className={editing === provider.id ? "active" : ""}
                    onClick={() => choose(provider)}
                  >
                    <span className="provider-avatar">
                      {provider.name.slice(0, 1)}
                    </span>
                    <span>
                      <strong>{provider.name}</strong>
                      <small>
                        {state.active?.provider_id === provider.id
                          ? state.active.model
                          : `${provider.models.length} 个模型`}
                      </small>
                    </span>
                    {state.active?.provider_id === provider.id && (
                      <span className="selected-dot" />
                    )}
                  </button>
                ))}
                <button
                  className="add-provider"
                  disabled={!!busy}
                  onClick={() => choose(null)}
                >
                  <Plus size={17} />
                  添加服务商
                </button>
              </aside>
              <section className="provider-editor">
                <div className="provider-title">
                  <div>
                    <h3>{editing ? draft.name : "添加模型服务商"}</h3>
                    <p>
                      {editing
                        ? "配置连接信息，管理可用模型。"
                        : "选择常用服务商，或接入兼容的自定义接口。"}
                    </p>
                  </div>
                  <Server size={23} />
                </div>
                {!editing && (
                  <div className="provider-presets">
                    {presets.map((preset) => (
                      <button
                        key={preset.id}
                        disabled={!!busy}
                        onClick={() => {
                          setDraft({
                            ...empty(),
                            name: preset.name,
                            base_url: preset.url,
                            protocol: preset.protocol,
                          });
                          setModels([]);
                        }}
                      >
                        {preset.name}
                      </button>
                    ))}
                    <button
                      disabled={!!busy}
                      onClick={() => {
                        setDraft(empty());
                        setModels([]);
                      }}
                    >
                      自定义
                    </button>
                  </div>
                )}
                <form
                  onSubmit={(event) => {
                    event.preventDefault();
                    void save();
                  }}
                >
                  <fieldset disabled={!!busy} className="provider-fields">
                    <label className="provider-name">
                      显示名称
                      <input
                        value={draft.name}
                        required
                        maxLength={100}
                        onChange={(event) =>
                          setDraft({ ...draft, name: event.target.value })
                        }
                        placeholder="例如：我的 DeepSeek"
                      />
                      <small>方便自己辨认即可，支持中文；也可以为同一服务商添加多份配置。</small>
                    </label>
                    <label className="provider-address">
                      接口地址（Base URL）
                      <input
                        type="url"
                        required
                        value={draft.base_url}
                        onChange={(event) =>
                          setDraft({ ...draft, base_url: event.target.value })
                        }
                        placeholder="https://api.example.com/v1"
                      />
                    </label>
                    <label className="provider-protocol">
                      API 协议
                      <select
                        value={draft.protocol}
                        onChange={(event) =>
                          setDraft({
                            ...draft,
                            protocol: event.target
                              .value as Provider["protocol"],
                          })
                        }
                      >
                        <option value="chat_completions">
                          OpenAI Chat Completions
                        </option>
                        <option value="responses">OpenAI Responses</option>
                        <option value="anthropic_messages">
                          Anthropic Messages
                        </option>
                      </select>
                    </label>
                    <label className="provider-key">
                      API 密钥
                      <div className="password-field">
                        <input
                          type={visible ? "text" : "password"}
                          autoComplete="new-password"
                          value={key}
                          onChange={(event) => setKey(event.target.value)}
                          placeholder={
                            draft.api_key_set
                              ? "已保存，留空即可保持原密钥"
                              : "输入服务商提供的 API Key"
                          }
                        />
                        <button
                          type="button"
                          className="icon-button"
                          aria-label={visible ? "隐藏密钥" : "显示密钥"}
                          onClick={() => setVisible(!visible)}
                        >
                          {visible ? <EyeOff size={17} /> : <Eye size={17} />}
                        </button>
                      </div>
                    </label>
                    <div className="models-heading">
                      <div>
                        <h4>可用模型</h4>
                        <p>勾选你想使用的模型，也可以手动添加。</p>
                      </div>
                      <button
                        type="button"
                        className="button"
                        disabled={!draft.base_url}
                        onClick={discover}
                      >
                        {busy === "discover" ? (
                          <LoaderCircle size={15} className="spin" />
                        ) : (
                          <Download size={15} />
                        )}
                        获取模型
                      </button>
                    </div>
                    {models.length > 0 && (
                      <>
                        <label className="search-input compact">
                          <Search size={16} />
                          <input
                            aria-label="搜索模型"
                            placeholder="搜索模型名称"
                            value={query}
                            onChange={(event) => setQuery(event.target.value)}
                          />
                        </label>
                        <div className="model-options">
                          {filtered.map((model) => (
                            <label key={model.id}>
                              <input
                                type="checkbox"
                                checked={draft.models.some(
                                  (item) => item.id === model.id,
                                )}
                                onChange={(event) =>
                                  setDraft({
                                    ...draft,
                                    models: event.target.checked
                                      ? [...draft.models, model]
                                      : draft.models.filter(
                                          (item) => item.id !== model.id,
                                        ),
                                  })
                                }
                              />
                              <span>
                                {model.name || model.id}
                                <small>
                                  {model.name !== model.id ? model.id : ""}
                                </small>
                              </span>
                              {state?.active?.provider_id === editing &&
                                state.active.model === model.id && (
                                  <small className="active-model-tag">
                                    默认模型
                                  </small>
                                )}
                            </label>
                          ))}
                          {!filtered.length && (
                            <p className="muted">没有匹配的模型。</p>
                          )}
                        </div>
                      </>
                    )}
                    <div className="manual-model">
                      <input
                        aria-label="手动添加模型 ID"
                        value={manual}
                        onChange={(event) => setManual(event.target.value)}
                        placeholder="手动输入模型 ID"
                        onKeyDown={(event) => {
                          if (event.key === "Enter") event.preventDefault();
                        }}
                      />
                      <button
                        type="button"
                        className="button"
                        disabled={!manual.trim()}
                        onClick={() => {
                          const id = manual.trim();
                          if (!draft.models.some((item) => item.id === id))
                            setDraft({
                              ...draft,
                              models: [...draft.models, { id, name: id }],
                            });
                          if (!models.some((item) => item.id === id))
                            setModels([...models, { id, name: id }]);
                          setManual("");
                        }}
                      >
                        <Plus size={15} />
                        添加
                      </button>
                    </div>
                    <button
                      className="button primary save-provider"
                      type="submit"
                    >
                      {busy === "save" ? (
                        <LoaderCircle size={16} className="spin" />
                      ) : (
                        <Check size={16} />
                      )}
                      保存配置
                    </button>
                  </fieldset>
                </form>
                {editing && savedModels.length > 0 && (
                  <section className="use-model">
                    <h4>默认模型</h4>
                    <p className="default-model-note">
                      未单独指定模型的功能会使用这里的默认选择。
                    </p>
                    {savedModels.map((model) => (
                      <div key={model.id}>
                        <span>{model.name || model.id}</span>
                        <button
                          className="text-button"
                          disabled={!!busy}
                          onClick={() => activate(model)}
                        >
                          {state?.active?.provider_id === editing &&
                          state.active.model === model.id ? (
                            <>
                              <Check size={14} />
                              默认模型
                            </>
                          ) : (
                            <>
                              设为默认
                              <ChevronRight size={14} />
                            </>
                          )}
                        </button>
                      </div>
                    ))}
                    <button
                      className="button test-model"
                      disabled={!!busy}
                      onClick={() =>
                        action("test", async () => {
                          const model =
                            state?.active?.provider_id === editing
                              ? state.active.model
                              : savedModels[0].id;
                          const result = await post<{
                            ok: boolean;
                            message: string;
                          }>(
                            `/model-providers/${encodeURIComponent(editing)}/test`,
                            { model },
                          );
                          if (result.ok)
                            setMessage(
                              result.message || "连接正常，可以开始对话。",
                            );
                          else
                            setError(
                              result.message || "连接未成功，请检查配置。",
                            );
                        })
                      }
                    >
                      {busy === "test" && (
                        <LoaderCircle size={15} className="spin" />
                      )}
                      测试连接
                    </button>
                  </section>
                )}
                {editing && (
                  <button
                    className="delete-provider text-button"
                    disabled={!!busy}
                    onClick={() => {
                      if (!deleteConfirm) {
                        setDeleteConfirm(true);
                        return;
                      }
                      void action("delete", async () => {
                        await api(
                          `/model-providers/${encodeURIComponent(editing)}`,
                          { method: "DELETE" },
                        );
                        const result = await reload();
                        choose(result.providers[0] || null);
                        await onChanged();
                        setMessage("服务商已移除。");
                      });
                    }}
                  >
                    <Trash2 size={14} />
                    {deleteConfirm ? "再次点击确认移除" : "移除此服务商"}
                  </button>
                )}
              </section>
            </div>
          )}
        </>
      )}
    </div>
  );
}
