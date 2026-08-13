import type { AgentEvent, AgentSettings, ProviderInfo } from "@/types";
import { API_URL, ApiError, apiRequest } from "./client";

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
 * Runs the agent for one user turn and consumes its `data:`-only SSE stream,
 * forwarding every event (thought / tool_start / tool_end / final / done).
 */
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
    throw new ApiError(`Cannot reach the Myra backend at ${API_URL}.`, 0);
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
