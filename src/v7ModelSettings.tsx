import { useEffect, useState } from "react";
import {
  Check,
  CheckCircle2,
  ChevronRight,
  Download,
  Eye,
  EyeOff,
  LoaderCircle,
  Plus,
  Search,
  Server,
  Trash2,
} from "lucide-react";
import { api, errorText, post, put } from "./api";
import { Spinner } from "./components";

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
}
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
  const choose = (provider: Provider | null) => {
    setDraft(provider || empty());
    setEditing(provider?.id || null);
    setKey("");
    setVisible(false);
    setModels(provider?.models || []);
    setQuery("");
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
        id: draft.id.trim(),
        name: draft.name.trim(),
        base_url: draft.base_url.trim(),
        protocol: draft.protocol,
        ...(key ? { api_key: key } : {}),
        models: draft.models,
      };
      if (editing)
        await put(`/model-providers/${encodeURIComponent(editing)}`, body);
      else await post("/model-providers", body);
      await reload(body.id);
      await onChanged();
      setMessage("服务商已保存。选择一个模型，即可开始对话。");
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
      setMessage(`已启用 ${model.name || model.id}。`);
    });
  const filtered = models.filter((item) =>
    `${item.id} ${item.name}`.toLowerCase().includes(query.toLowerCase()),
  );
  const savedModels =
    state?.providers.find((provider) => provider.id === editing)?.models || [];
  return (
    <div className="model-settings">
      <p className="settings-intro">连接你自己的模型，让看台为你读懂足球。</p>
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
        <div className="provider-layout">
          <aside className="provider-list">
            <span className="small-heading">服务商</span>
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
                        id: preset.id,
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
                <div className="field-pair">
                  <label>
                    显示名称
                    <input
                      value={draft.name}
                      required
                      maxLength={80}
                      onChange={(event) =>
                        setDraft({ ...draft, name: event.target.value })
                      }
                      placeholder="例如：我的 DeepSeek"
                    />
                  </label>
                  <label>
                    服务商 ID
                    <input
                      value={draft.id}
                      autoComplete="username"
                      required
                      pattern="([a-zA-Z0-9_]|-)+"
                      maxLength={64}
                      disabled={!!editing}
                      onChange={(event) =>
                        setDraft({ ...draft, id: event.target.value })
                      }
                      placeholder="例如：deepseek"
                    />
                    <small>
                      {editing
                        ? "创建后保持不变"
                        : "使用英文字母、数字、短横线"}
                    </small>
                  </label>
                </div>
                <label>
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
                <label>
                  API 协议
                  <select
                    value={draft.protocol}
                    onChange={(event) =>
                      setDraft({
                        ...draft,
                        protocol: event.target.value as Provider["protocol"],
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
                <label>
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
                                当前使用
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
                <button className="button primary save-provider" type="submit">
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
                <h4>开始使用</h4>
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
                          已启用
                        </>
                      ) : (
                        <>
                          使用此模型
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
                        setError(result.message || "连接未成功，请检查配置。");
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
    </div>
  );
}
