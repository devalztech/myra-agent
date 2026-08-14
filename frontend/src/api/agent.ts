import type { AgentEvent, AgentSettings, ProviderInfo } from "@/types";
import { API_URL, ApiError, apiRequest, errorMessage } from "./client";

export function fetchProviders(): Promise<{ providers: ProviderInfo[]; default: string }> {
  return apiRequest<{ providers: ProviderInfo[]; default: string }>("/providers");
}

export function fetchAgentSettings(token: string): Promise<AgentSettings> {
  return apiRequest<AgentSettings>("/settings", { token });
}

export function updateAgentSettings(
  token: string,
  patch: Partial<Pick<AgentSettings, "provider" | "approvalRequired" | "maxToolCalls" | "agentMode">>,
): Promise<AgentSettings> {
  return apiRequest<AgentSettings>("/settings", { method: "PATCH", token, body: patch });
}

export function fetchSessionEvents(
  token: string,
  sessionId: string,
): Promise<{ events: (AgentEvent & { messageId?: string })[] }> {
  return apiRequest<{ events: (AgentEvent & { messageId?: string })[] }>(
    `/sessions/${sessionId}/events`,
    { token },
  );
}

/**
 * Explicit stop, distinct from just aborting the fetch. A dropped
 * connection alone no longer halts a run server-side (it finishes
 * unattended so a flaky connection can't strand it mid-task) — this is
 * what actually tells the backend "the user asked to stop", so it must be
 * called (and awaited, or at least fired) before the AbortController is
 * used to close the stream. Best-effort: if it fails to send (e.g. already
 * offline), the run simply keeps going and finishes on its own, which is
 * the safe default anyway.
 */
export function stopAgent(token: string, sessionId: string): Promise<void> {
  return apiRequest<void>(`/sessions/${sessionId}/stop`, { method: "POST", token }).catch(
    () => undefined,
  );
}

/**
 * Downloads a file from the agent's workspace, authenticated with the
 * user's token. A plain <a href> to /workspace/download won't work — that
 * endpoint requires a Bearer token header, which a normal browser
 * navigation/click never attaches — so this fetches the file as a blob and
 * triggers the save via a throwaway object URL instead.
 */
export async function downloadWorkspaceFile(token: string, path: string): Promise<void> {
  let response: Response;
  try {
    response = await fetch(
      `${API_URL}/workspace/download?path=${encodeURIComponent(path)}`,
      { headers: { Authorization: `Bearer ${token}` } },
    );
  } catch {
    throw new ApiError("Cannot reach the Myra backend. Please try again.", 0);
  }
  if (!response.ok) {
    throw new ApiError(await errorMessage(response), response.status);
  }
  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = path.split("/").pop() || "download";
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
}
export async function runAgent(
  token: string,
  sessionId: string,
  content: string,
  options: { provider?: string; approved?: boolean; signal?: AbortSignal },
  onEvent: (event: AgentEvent) => void,
): Promise<void> {
  let response: Response;
  try {
    response = await fetch(`${API_URL}/sessions/${sessionId}/agent`, {
      method: "POST",
      signal: options.signal ?? null,
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
      body: JSON.stringify({
        content,
        provider: options.provider ?? null,
        approved: options.approved ?? false,
      }),
    });
  } catch {
    throw new ApiError("Cannot reach the Myra backend. Please try again.", 0);
  }

  if (!response.ok || !response.body) {
    throw new ApiError(response.statusText || "Agent run failed.", response.status);
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    let split = buffer.indexOf("\n\n");
    while (split !== -1) {
      const frame = buffer.slice(0, split);
      buffer = buffer.slice(split + 2);
      split = buffer.indexOf("\n\n");

      const data = frame
        .split("\n")
        .filter((line) => line.startsWith("data:"))
        .map((line) => line.slice(5).trim())
        .join("\n");
      if (!data) continue;
      try {
        onEvent(JSON.parse(data) as AgentEvent);
      } catch {
        /* ignore malformed frame */
      }
    }
  }
}
