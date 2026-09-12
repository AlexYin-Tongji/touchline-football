export type Mode = "demo" | "live";
export type Page =
  "home" | "matches" | "tracking" | "chat" | "sources" | "settings";
export type Skill =
  | "auto"
  | "ask"
  | "team_brief"
  | "match_preview"
  | "match_review"
  | "transfer_check"
  | "tactics_explainer"
  | "event_watch";
export interface Team {
  id: string;
  name: string;
  short_name?: string;
  english_name?: string;
  color?: string;
  league?: string;
  league_name?: string;
  crest_url?: string;
  country?: string;
  city?: string;
  founded?: number;
  stadium?: string;
  nickname?: string;
  description?: string;
  official_url?: string;
  profile_sources?: { name: string; url: string }[];
}
export interface Evidence {
  id: string;
  mode: Mode;
  team_id: string;
  title: string;
  text: string;
  url?: string;
  source_name?: string;
  published_at?: string | null;
  fetched_at?: string;
  source_id?: string | null;
  origin?: string;
  spoiler_redacted?: boolean;
  spoiler_hidden?: boolean;
  redaction_note?: string;
  spoiler?: boolean;
  fixture_record?: boolean;
  content_status?: "full_text" | "partial" | "summary" | "imported";
  content_method?: string;
  content_note?: string;
  paragraph_count?: number;
  word_count?: number;
  reading_minutes?: number;
  text_truncated?: boolean;
  paragraphs?: { index: number; text: string }[];
}
export interface ClubEvent {
  id: string;
  mode: Mode;
  team_id: string;
  title: string;
  display_title?: string;
  display_summary?: string;
  summary_origin?: "assistant" | "source" | "pending";
  display_status?: "supported" | "unverified" | "pending";
  source_run_id?: string | null;
  summary?: string;
  change?: string;
  kind?: string;
  stage?: string;
  importance?: string;
  updated_at?: string;
  evidence_ids: string[];
  watching?: boolean;
  read?: boolean;
  spoiler?: boolean;
  timeline?: { at?: string; text: string; evidence_id?: string }[];
  uncertainties?: string[];
}
export interface Match {
  id: string;
  mode: Mode;
  team_id: string;
  opponent: string;
  competition?: string;
  kickoff?: string;
  venue?: string;
  status?: string;
  score?: string | null;
  note?: string;
  evidence_ids?: string[];
  spoiler_hidden?: boolean;
  source_url?: string;
  source_name?: string;
  source_match_id?: string;
  updated_at?: string;
  synced_at?: string;
  kickoff_confirmed?: boolean;
  data_warnings?: string[];
  kickoff_date?: string;
  fixture_status?: string;
  goals_for?: number;
  goals_against?: number;
  result?: "W" | "D" | "L";
  lineups?: {
    status: "confirmed" | "unavailable";
    home?: string[];
    away?: string[];
    source_url?: string;
    updated_at?: string;
  };
  availability?: {
    name: string;
    status: string;
    source_url?: string;
    evidence_id?: string;
    updated_at?: string;
  }[];
}
export interface Source {
  id: string;
  mode: Mode;
  team_id: string;
  name: string;
  url: string;
  kind: "html" | "rss";
  enabled: boolean;
  last_checked?: string | null;
  last_success?: string | null;
  last_added?: number;
  last_error?: string | null;
}
export interface Settings {
  mode: Mode;
  followed_teams: string[];
  spoiler_free: boolean;
  auto_refresh: boolean;
  refresh_minutes: number;
  explanation_depth?: "brief" | "standard" | "deep";
  personalization?: {
    interests: string[];
    reading_language: "zh" | "original";
    fan_level: "beginner" | "regular" | "expert";
    briefing_window_hours: number;
  };
  automation?: {
    enabled: boolean;
    interval_minutes: number;
    sync_matches: boolean;
    generate_brief: boolean;
  };
  verification_enabled?: boolean;
  verification_timeout_seconds?: number;
  llm: {
    thinking_enabled?: boolean;
    protocol?: "chat_completions" | "responses";
    send_temperature?: boolean;
    max_output_tokens?: number;
    use_env_proxy?: boolean;
    base_url: string;
    model: string;
    api_key_set: boolean;
    max_rounds: number;
    timeout_seconds: number;
    temperature: number;
  };
}
export interface Run {
  id: string;
  mode: Mode;
  team_id: string;
  match_id?: string | null;
  conversation_id: string;
  skill: Skill;
  question: string;
  status: "queued" | "running" | "completed" | "failed" | "cancelled";
  answer?: string;
  evidence_ids?: string[];
  evidence?: Evidence[];
  warnings?: string[];
  steps?: { label: string; status: string; detail?: string; at?: string }[];
  error?: string | null;
  created_at?: string;
  finished_at?: string | null;
  model?: string;
  skills_used?: string[];
  protocol?: string;
  parent_run_id?: string;
  structured_answer?: {
    summary?: string;
    facts?: Array<
      | string
      | {
          title?: string;
          detail?: string;
          text?: string;
          evidence_ids?: string[];
          quotes?: { evidence_id: string; quote: string }[];
        }
    >;
    analysis?: string | unknown[];
    unknowns?: unknown[];
  };
  verification?: {
    status:
      | "supported"
      | "partial"
      | "insufficient"
      | "unavailable"
      | "not_requested";
    checked_at?: string;
    claims: {
      id: string;
      text: string;
      verdict: "supported" | "unsupported" | "conflicting" | "uncertain";
      reason: string;
      quotes: { evidence_id: string; quote: string }[];
    }[];
    warnings: string[];
  };
}
export interface Recommendation {
  id: string;
  team_id: string;
  mode: Mode;
  kind: "read" | "preview" | "review" | "follow" | "verify" | "setup";
  title: string;
  reason: string;
  priority: "high" | "normal";
  evidence_ids: string[];
  match_id?: string;
  event_id?: string;
  query?: string;
  created_at: string;
  expires_at?: string;
  dismissed?: boolean;
}
export interface Tracking {
  id: string;
  mode: "live";
  team_id: string;
  title: string;
  query: string;
  event_id?: string;
  match_id?: string;
  enabled: boolean;
  interval_minutes: number;
  last_checked?: string | null;
  next_check?: string | null;
  last_run_id?: string | null;
  status: "idle" | "running" | "changed" | "unchanged" | "failed" | "paused";
  last_error?: string | null;
  summary: string;
  created_at: string;
  updates: {
    at: string;
    run_id: string;
    evidence_ids: string[];
    summary: string;
  }[];
}
export interface Candidate {
  id: string;
  team_id: string;
  url: string;
  title: string;
  source_name: string;
  published_at?: string;
}
export interface CatalogSource {
  id: string;
  name: string;
  url: string;
  kind: string;
  description?: string;
}
export interface ModelTest {
  ok: boolean;
  protocol?: string;
  tool_support?: boolean | null;
  message: string;
  checks?: { stage: string; ok: boolean; message: string }[];
  elapsed_ms?: number;
}
export interface AppState {
  runtime?: { server_mode: boolean; storage: "server" | "local" };
  settings: Settings;
  teams: Team[];
  events: ClubEvent[];
  evidence: Evidence[];
  matches: Match[];
  sources: Source[];
  runs: Run[];
  tracking?: Tracking[];
  source_catalog?: CatalogSource[];
  setup?: { model_configured: boolean; live_evidence_count: number };
  last_refresh?: string | null;
  spoiler_hidden?: { events?: number; evidence?: number; runs?: number };
  recommendations?: Recommendation[];
  automation_status?: {
    last_checked?: string;
    last_error?: string;
    running?: boolean;
    next_check?: string;
    last_run_id?: string;
  };
  service?: { version: string; running: boolean; autostart_enabled: boolean; server_mode?: boolean };
}
export interface Capabilities {
  tools: { name: string; description: string }[];
  skills: { id: string; name: string; description: string }[];
}
export interface RefreshResult {
  added: number;
  unchanged: number;
  errors: string[];
}
