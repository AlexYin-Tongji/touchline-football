import { useEffect, useRef, useState, type FormEvent } from "react";
import { Save } from "lucide-react";
import { errorText, patch } from "./api";
import { ErrorNotice, Spinner } from "./components";
import type { Team } from "./types";

interface TeamProfileEditorProps {
  team: Team;
  onSaved: () => Promise<void>;
  notice: (text: string, error?: boolean) => void;
}

interface ProfileDraft {
  nickname: string;
  city: string;
  founded: string;
  stadium: string;
  description: string;
  official_url: string;
  crest_url: string;
}

function draftFromTeam(team: Team): ProfileDraft {
  return {
    nickname: team.nickname ?? "",
    city: team.city ?? "",
    founded: team.founded == null ? "" : String(team.founded),
    stadium: team.stadium ?? "",
    description: team.description ?? "",
    official_url: team.official_url ?? "",
    crest_url: team.crest_url ?? "",
  };
}

function isWebAddress(value: string) {
  try {
    const url = new URL(value);
    return (
      ["https:", "http:"].includes(url.protocol) &&
      !url.username &&
      !url.password
    );
  } catch {
    return false;
  }
}

function validationError(draft: ProfileDraft) {
  const founded = draft.founded.trim();
  if (
    founded &&
    (!/^\d{4}$/.test(founded) ||
      Number(founded) < 1800 ||
      Number(founded) > 2100)
  ) {
    return "成立年份请填写 1800 至 2100 之间的整数，也可以留空。";
  }
  const website = draft.official_url.trim();
  if (website && !isWebAddress(website)) {
    return "请填写完整的官网网址，以 https:// 或 http:// 开头。";
  }
  const crest = draft.crest_url.trim();
  if (crest.startsWith("/assets/")) {
    if (
      /[\\%?#]/.test(crest) ||
      crest.split("/").includes("..") ||
      !/\.(png|svg|jpe?g|webp|avif)$/i.test(crest)
    ) {
      return "请填写有效的本地队徽图片地址。";
    }
  } else if (crest && !isWebAddress(crest)) {
    return "队徽地址请填写完整的图片网址，或以 /assets/ 开头的本地图片路径。";
  }
  return "";
}

export function TeamProfileEditor(props: TeamProfileEditorProps) {
  return <ProfileForm key={props.team.id} {...props} />;
}

function ProfileForm({ team, onSaved, notice }: TeamProfileEditorProps) {
  const [draft, setDraft] = useState(() => draftFromTeam(team));
  const [savedDraft, setSavedDraft] = useState(() => draftFromTeam(team));
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const inFlight = useRef(false);
  const mounted = useRef(true);
  const dirty = (Object.keys(draft) as (keyof ProfileDraft)[]).some(
    (field) => draft[field].trim() !== savedDraft[field].trim(),
  );

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  // Background refreshes may update the profile, but must preserve an open edit.
  useEffect(() => {
    if (!dirty && !inFlight.current) {
      const next = draftFromTeam(team);
      setDraft(next);
      setSavedDraft(next);
    }
  }, [team]);

  function update(field: keyof ProfileDraft, value: string) {
    setDraft((current) => ({ ...current, [field]: value }));
    setError("");
  }

  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (inFlight.current || !dirty) return;
    const invalid = validationError(draft);
    if (invalid) {
      setError(invalid);
      return;
    }
    const changes: Partial<Record<keyof ProfileDraft, string | number | null>> =
      {};
    for (const field of Object.keys(draft) as (keyof ProfileDraft)[]) {
      const value = draft[field].trim();
      if (value !== savedDraft[field].trim()) {
        changes[field] =
          field === "founded" ? (value ? Number(value) : null) : value || null;
      }
    }
    inFlight.current = true;
    setSaving(true);
    setError("");
    let persisted = false;
    try {
      const saved = await patch<Team>(
        `/teams/${encodeURIComponent(team.id)}/profile`,
        changes,
      );
      persisted = true;
      if (mounted.current) {
        const next = draftFromTeam(saved);
        setDraft(next);
        setSavedDraft(next);
      }
      await onSaved();
      notice(`${team.name}的资料已保存。`);
    } catch (cause) {
      const detail = errorText(cause).replace(/^Value error,\s*/, "");
      const message = persisted
        ? "球队资料已保存，页面暂时未能更新，请刷新后查看。"
        : /[\u3400-\u9fff]/.test(detail)
          ? detail
          : "球队资料未能保存，请确认本地服务正常后重试。";
      if (mounted.current) setError(message);
      notice(message, true);
    } finally {
      inFlight.current = false;
      if (mounted.current) setSaving(false);
    }
  }

  return (
    <form
      className="panel form-panel"
      onSubmit={save}
      noValidate
      aria-busy={saving}
    >
      <h2>{team.name}的资料</h2>
      <ErrorNotice>{error}</ErrorNotice>
      <fieldset disabled={saving}>
        <div className="form-grid">
          <label>
            球队昵称
            <input
              value={draft.nickname}
              onChange={(e) => update("nickname", e.target.value)}
              maxLength={100}
            />
          </label>
          <label>
            所在城市
            <input
              value={draft.city}
              onChange={(e) => update("city", e.target.value)}
              maxLength={100}
            />
          </label>
          <label>
            成立年份
            <input
              value={draft.founded}
              onChange={(e) => update("founded", e.target.value)}
              inputMode="numeric"
              maxLength={4}
            />
          </label>
          <label>
            主场球场
            <input
              value={draft.stadium}
              onChange={(e) => update("stadium", e.target.value)}
              maxLength={100}
            />
          </label>
        </div>
        <label className="spaced">
          球队简介
          <textarea
            value={draft.description}
            onChange={(e) => update("description", e.target.value)}
            rows={4}
            maxLength={1500}
          />
        </label>
        <label className="spaced">
          官方网站
          <input
            value={draft.official_url}
            onChange={(e) => update("official_url", e.target.value)}
            type="url"
            maxLength={4096}
            placeholder="填写俱乐部官网网址"
          />
        </label>
        <label className="spaced">
          队徽图片地址
          <input
            value={draft.crest_url}
            onChange={(e) => update("crest_url", e.target.value)}
            maxLength={4096}
            placeholder="填写图片网址或本地图片路径"
          />
        </label>
        <div className="form-actions">
          <button
            className="button"
            type="button"
            disabled={!dirty}
            onClick={() => {
              setDraft(savedDraft);
              setError("");
            }}
          >
            撤销修改
          </button>
          <button className="button primary" type="submit" disabled={!dirty}>
            {saving ? (
              <Spinner text="保存中" />
            ) : (
              <>
                <Save size={15} />
                保存球队资料
              </>
            )}
          </button>
        </div>
      </fieldset>
    </form>
  );
}
