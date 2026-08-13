/** Shared types matching the FastAPI backend schemas. */

export type User = {
  id: string;
  name: string;
  email: string;
};

export type AuthResponse = {
  token: string;
  user: User;
};

export type LoginPayload = {
  email: string;
  password: string;
};

export type RegisterPayload = {
  name: string;
  email: string;
  password: string;
};

export type ChatRole = "user" | "assistant" | "system";

export type ChatMessage = {
  id: string;
  role: ChatRole;
  content: string;
  createdAt: string;
};

/** Sidebar row — no messages loaded. */
export type SessionSummary = {
  id: string;
  title: string;
  updatedAt: string;
};

export type ChatSession = SessionSummary & {
  messages: ChatMessage[];
};

export type ChatResponse = {
  session: SessionSummary;
  userMessage: ChatMessage;
  assistantMessage: ChatMessage;
};

export type ModelStatus = {
  backend: string;
  model: string | null;
  loaded: boolean;
  contextSize: number;
  ramGb: number;
  tier: string;
  /** idle | downloading | loading | ready | error */
  status?: string;
  detail?: string | null;
  threads?: number;
};

// --------------------------------------------------------------------------
// agent
// --------------------------------------------------------------------------

export type ProviderInfo = {
  id: string;
  name: string;
  kind: "local" | "remote" | "mock" | string;
  model: string | null;
  available: boolean;
  detail: string | null;
};

export type AgentLimits = {
  maxToolCalls: number;
  maxSteps: number;
  toolTimeoutSeconds: number;
  agentTimeoutSeconds: number;
};

export type AgentSettings = {
  provider: string;
  approvalRequired: boolean;
  maxToolCalls: number;
  agentMode: boolean;
  limits: AgentLimits;
};

/** Raw SSE frame from `POST /sessions/{id}/agent`. */
export type AgentEvent = {
  type: string;
  [key: string]: unknown;
};

export type ActivityStatus = "pending" | "running" | "done" | "error" | "blocked";

export type ActivityStep = {
  id: string;
  label: string;
  detail?: string;
  status: ActivityStatus;
};

/** An assistant turn plus the agent steps that produced it. */
export type Turn = {
  message: ChatMessage;
  steps?: ActivityStep[];
};

export type WorkspaceInfo = {
  root?: string;
  [key: string]: unknown;
};
