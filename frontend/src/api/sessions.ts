import type { ChatResponse, ChatSession, ModelStatus, SessionSummary } from "@/types";
import { apiRequest, apiStream, type SseEvent } from "./client";

export function fetchSessions(token: string): Promise<SessionSummary[]> {
  return apiRequest<SessionSummary[]>("/sessions", { token });
}

export function fetchSession(token: string, id: string): Promise<ChatSession> {
  return apiRequest<ChatSession>(`/sessions/${id}`, { token });
}

export function createSession(token: string, title?: string): Promise<ChatSession> {
  return apiRequest<ChatSession>("/sessions", {
    method: "POST",
    token,
    body: { title: title ?? null },
  });
}

export function renameSession(token: string, id: string, title: string): Promise<SessionSummary> {
  return apiRequest<SessionSummary>(`/sessions/${id}`, {
    method: "PATCH",
    token,
    body: { title },
  });
}

export function deleteSession(token: string, id: string): Promise<void> {
  return apiRequest<void>(`/sessions/${id}`, { method: "DELETE", token });
}

/** Non-streaming fallback. */
export function sendMessage(token: string, id: string, content: string): Promise<ChatResponse> {
  return apiRequest<ChatResponse>(`/sessions/${id}/chat`, {
    method: "POST",
    token,
    body: { content },
  });
}

export type StreamHandlers = {
  onSession?: (session: { id: string; title: string }) => void;
  onToken?: (token: string) => void;
  onDone?: (assistant: { id: string; content: string; createdAt: string }) => void;
  onError?: (message: string) => void;
};

export async function streamMessage(
  token: string,
  id: string,
  content: string,
  handlers: StreamHandlers,
  signal?: AbortSignal,
): Promise<void> {
  await apiStream(
    `/sessions/${id}/chat/stream`,
    { token, body: { content }, ...(signal ? { signal } : {}) },
    ({ event, data }: SseEvent) => {
      const payload = data as Record<string, string>;
      if (event === "session") handlers.onSession?.({ id: payload["id"]!, title: payload["title"]! });
      else if (event === "token") handlers.onToken?.(payload["token"] ?? "");
      else if (event === "assistant_message")
        handlers.onDone?.({
          id: payload["id"]!,
          content: payload["content"] ?? "",
          createdAt: payload["createdAt"]!,
        });
      else if (event === "error") handlers.onError?.(payload["message"] ?? "Inference failed.");
    },
  );
}

export function fetchModelStatus(): Promise<ModelStatus> {
  return apiRequest<ModelStatus>("/model");
}
