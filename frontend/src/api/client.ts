/**
 * Thin API client for the Myra FastAPI backend.
 *
 * The backend is hosted separately (Pterodactyl panel + Cloudflare tunnel),
 * so its base URL comes from VITE_API_URL.
 */

export const API_URL: string = (
  (import.meta.env["VITE_API_URL"] as string | undefined) ?? "http://localhost:8000"
).replace(/\/+$/, "");

export class ApiError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

type RequestOptions = {
  method?: "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
  body?: unknown;
  token?: string | null;
  signal?: AbortSignal;
};

/** FastAPI returns `{ detail: string | [{ msg }] }` on errors. */
export async function errorMessage(response: Response): Promise<string> {
  try {
    const data = (await response.json()) as { detail?: unknown };
    const detail = data?.detail;
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail)) {
      const first = detail[0] as { msg?: string } | undefined;
      if (first?.msg) return first.msg;
    }
  } catch {
    /* non-JSON body */
  }
  if (response.status === 0) return "Cannot reach the Myra backend.";
  return response.statusText || `Request failed (${response.status})`;
}

function authHeaders(token?: string | null): Record<string, string> {
  return {
    "Content-Type": "application/json",
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
  };
}

export async function apiRequest<TResponse>(
  path: string,
  { method = "GET", body, token, signal }: RequestOptions = {},
): Promise<TResponse> {
  let response: Response;
  try {
    response = await fetch(`${API_URL}${path}`, {
      method,
      signal: signal ?? null,
      headers: authHeaders(token),
      ...(body !== undefined ? { body: JSON.stringify(body) } : {}),
    });
  } catch {
    throw new ApiError(`Cannot reach the Myra backend at ${API_URL}.`, 0);
  }

  if (!response.ok) {
    throw new ApiError(await errorMessage(response), response.status);
  }

  if (response.status === 204) return undefined as TResponse;
  return (await response.json()) as TResponse;
}

export type SseEvent = { event: string; data: unknown };

/**
 * POSTs and consumes a `text/event-stream` response from the backend.
 * Used for token-by-token chat streaming.
 */
export async function apiStream(
  path: string,
  { body, token, signal }: Omit<RequestOptions, "method">,
  onEvent: (event: SseEvent) => void,
): Promise<void> {
  let response: Response;
  try {
    response = await fetch(`${API_URL}${path}`, {
      method: "POST",
      signal: signal ?? null,
      headers: authHeaders(token),
      ...(body !== undefined ? { body: JSON.stringify(body) } : {}),
    });
  } catch {
    throw new ApiError(`Cannot reach the Myra backend at ${API_URL}.`, 0);
  }

  if (!response.ok || !response.body) {
    throw new ApiError(await errorMessage(response), response.status);
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
      const raw = buffer.slice(0, split);
      buffer = buffer.slice(split + 2);
      split = buffer.indexOf("\n\n");

      let eventName = "message";
      const dataLines: string[] = [];
      for (const line of raw.split("\n")) {
        if (line.startsWith("event:")) eventName = line.slice(6).trim();
        else if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());
      }
      if (dataLines.length === 0) continue;
      try {
        onEvent({ event: eventName, data: JSON.parse(dataLines.join("\n")) });
      } catch {
        /* ignore malformed frame */
      }
    }
  }
}
