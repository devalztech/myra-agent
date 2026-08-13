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
