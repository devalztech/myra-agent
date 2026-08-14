import type { AgentEvent, AgentSettings, ProviderInfo } from "@/types";
import { API_URL, ApiError, apiRequest, errorMessage } from "./client";

export function fetchProviders(): Promise<{ providers: ProviderInfo[]; default: string }> {
  return apiRequest<{ providers: ProviderInfo[]; default: string }>("/providers");
}

export function fetchAgentSettings(token: string): Promise<AgentSettings> {
  return apiRequest<AgentSettings>("/settings", { token });
}

export function fetchProviderConfigs(
  token: string,
): Promise<{ providers: Record<string, { apiKey?: string | null; baseUrl?: string | null; model?: string | null; hasKey: boolean }> }> {
  return apiRequest("/settings/providers", { token });
}

export function saveProviderConfig(
  token: string,
  provider: string,
  cfg: { apiKey?: string; baseUrl?: string; model?: string },
): Promise<{ provider: string; saved: boolean }> {
  return apiRequest(`/settings/providers/${provider}`, {
    method: "PUT",
    token,
    body: { provider, ...cfg },
  });
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

/** Uploads a file into the agent's workspace and returns its stored path. */
export async function uploadWorkspaceFile(
  token: string,
  file: File,
): Promise<{ path: string; bytes: number }> {
  const form = new FormData();
  form.append("file", file);
  let response: Response;
  try {
    response = await fetch(`${API_URL}/workspace/upload`, {
      method: "POST",
      headers: { Authorization: `Bearer ${token}` },
      body: form,
    });
  } catch {
    throw new ApiError("Cannot reach the Myra backend. Please try again.", 0);
  }
  if (!response.ok) {
    throw new ApiError(await errorMessage(response), response.status);
  }
  return (await response.json()) as { path: string; bytes: number };
}

export async function runAgent(
  token: string,
  sessionId: string,
  content: string,
  options: { provider?: string; approved?: boolean; signal?: AbortSignal },
  onEvent: (event: AgentEvent) => void,
): Promise<void> {
  const delay = (ms: number) => new Promise((r) => setTimeout(r, ms));
  const MAX_RETRIES = 2;

  for (let attempt = 0; attempt <= MAX_RETRIES; attempt += 1) {
    if (options.signal?.aborted) throw new Error("aborted");

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
      if (options.signal?.aborted) throw new Error("aborted");
      if (attempt < MAX_RETRIES) {
        await delay(1200 * (attempt + 1));
        continue;
      }
      throw new ApiError("Connection to Myra lost. Please try again.", 0);
    }

    if (!response.ok || !response.body) {
      throw new ApiError(response.statusText || "Agent run failed.", response.status);
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let sawDone = false;

    try {
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
          let event: AgentEvent;
          try {
            event = JSON.parse(data) as AgentEvent;
          } catch {
            continue;
          }
          if (event.type === "done") sawDone = true;
          onEvent(event);
        }
      }
    } catch {
      // Stream broke mid-flight (network drop). Retry unless the user
      // stopped or we already saw a clean terminal event.
      if (options.signal?.aborted || sawDone) throw new Error("aborted");
      if (attempt < MAX_RETRIES) {
        await delay(1200 * (attempt + 1));
        continue;
      }
      throw new ApiError("Connection to Myra was lost mid-response. Please try again.", 0);
    }
    return;
  }
}
