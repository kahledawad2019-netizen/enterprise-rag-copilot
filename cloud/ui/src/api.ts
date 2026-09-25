import type {
  AskResponse,
  DocumentsResponse,
  EvaluationResponse,
  HealthResponse,
  MetaResponse,
  RetrieveResponse,
  Strategy,
  StreamEvent,
  UploadResponse,
} from "./types";

/**
 * All network access lives here.
 *
 * The base URL is relative by default. In production the UI and the Worker sit
 * behind the same hostname, so a relative path means no CORS preflight. A
 * staging Pages deployment may set VITE_COPILOT_API_BASE to the full Worker
 * `/api` URL while the project does not yet have a shared custom domain.
 */
const BASE = (import.meta.env.VITE_COPILOT_API_BASE || "/api").replace(/\/$/, "");

type SessionTokenProvider = () => Promise<string | null>;

let sessionTokenProvider: SessionTokenProvider | null = null;

/**
 * Clerk owns the short-lived session token. Keeping the provider here lets
 * every API call fetch a fresh token without persisting it in localStorage or
 * threading authentication props through every view component.
 */
export function setSessionTokenProvider(provider: SessionTokenProvider | null): void {
  sessionTokenProvider = provider;
}

export class CopilotApiError extends Error {
  constructor(
    message: string,
    readonly code: string,
    readonly status: number,
  ) {
    super(message);
    this.name = "CopilotApiError";
  }
}

/**
 * Authorization header, when a sign-in provider is installed.
 *
 * Without Clerk (the local app and the public demo) there is no provider and
 * requests go out unauthenticated; the backend decides whether that is
 * allowed. With Clerk, a missing token is an error rather than a silent
 * anonymous request.
 */
async function authHeaders(): Promise<Record<string, string>> {
  if (!sessionTokenProvider) return {};
  const token = await sessionTokenProvider();
  if (!token) {
    throw new CopilotApiError(
      "Your session is not ready. Sign in and try again.",
      "unauthorized",
      401,
    );
  }
  return { Authorization: `Bearer ${token}` };
}

async function request<T>(path: string, init?: RequestInit, json = true): Promise<T> {
  const auth = await authHeaders();

  let response: Response;
  try {
    response = await fetch(`${BASE}${path}`, {
      ...init,
      headers: {
        ...(json ? { "Content-Type": "application/json" } : {}),
        ...auth,
        ...init?.headers,
      },
    });
  } catch {
    // A network-level failure reaches the user as a blank screen otherwise.
    throw new CopilotApiError(
      "Could not reach the copilot. Check your connection and try again.",
      "network",
      0,
    );
  }

  const text = await response.text();
  let payload: unknown = null;
  if (text) {
    try {
      payload = JSON.parse(text);
    } catch {
      throw new CopilotApiError(
        `The server returned a response that was not JSON (${response.status}).`,
        "bad_response",
        response.status,
      );
    }
  }

  if (!response.ok) {
    const body = payload as { error?: string; detail?: string } | null;
    throw new CopilotApiError(
      body?.detail ?? `Request failed with status ${response.status}.`,
      body?.error ?? "unknown",
      response.status,
    );
  }

  return payload as T;
}

export function getMeta(): Promise<MetaResponse> {
  return request<MetaResponse>("/meta");
}

export function getHealth(): Promise<HealthResponse> {
  return request<HealthResponse>("/health");
}

export interface AskOptions {
  question: string;
  persona: string;
  strategy: Strategy;
  signal?: AbortSignal;
}

export function ask({ question, persona, strategy, signal }: AskOptions): Promise<AskResponse> {
  return request<AskResponse>("/ask", {
    method: "POST",
    body: JSON.stringify({ question, persona, strategy }),
    signal,
  });
}

/**
 * Ask with a streamed answer. Calls `onEvent` for each server-sent event and
 * resolves with the final validated response.
 *
 * Uses fetch + a stream reader rather than EventSource, because EventSource
 * can only GET and cannot send an Authorization header.
 */
export async function askStream(
  { question, persona, strategy, signal }: AskOptions,
  onEvent: (event: StreamEvent) => void,
): Promise<AskResponse> {
  const auth = await authHeaders();
  let response: Response;
  try {
    response = await fetch(`${BASE}/ask/stream`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "text/event-stream", ...auth },
      body: JSON.stringify({ question, persona, strategy }),
      signal,
    });
  } catch (err) {
    if ((err as Error).name === "AbortError") throw err;
    throw new CopilotApiError(
      "Could not reach the copilot. Check your connection and try again.",
      "network",
      0,
    );
  }

  if (!response.ok || !response.body) {
    let detail = `Request failed with status ${response.status}.`;
    let code = "unknown";
    try {
      const body = (await response.json()) as { error?: string; detail?: string };
      detail = body.detail ?? detail;
      code = body.error ?? code;
    } catch {
      // Not JSON; keep the generic message.
    }
    throw new CopilotApiError(detail, code, response.status);
  }

  const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
  let buffer = "";
  let previousChunkEndedWithCr = false;
  let result: AskResponse | null = null;

  const processBlock = (block: string) => {
    const event = parseSse(block);
    if (!event) return;
    if (event.type === "error") {
      throw new CopilotApiError(event.detail, event.error, 500);
    }
    if (event.type === "done") result = event.result;
    onEvent(event);
  };

  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    if (!value) continue;
    // A CR already became a newline; swallow its LF even across chunks.
    const chunk = previousChunkEndedWithCr && value.startsWith("\n") ? value.slice(1) : value;
    previousChunkEndedWithCr = value.endsWith("\r");
    buffer += chunk.replace(/\r\n?/g, "\n");

    let boundary: number;
    while ((boundary = buffer.indexOf("\n\n")) >= 0) {
      const block = buffer.slice(0, boundary);
      buffer = buffer.slice(boundary + 2);
      processBlock(block);
    }
  }
  if (buffer) processBlock(buffer);

  if (!result) {
    throw new CopilotApiError("The answer stream ended unexpectedly.", "stream", 0);
  }
  return result;
}

function parseSse(block: string): StreamEvent | null {
  let kind = "";
  const data: string[] = [];
  for (const line of block.split("\n")) {
    if (line.startsWith(":")) continue; // keep-alive comment
    if (line.startsWith("event:")) kind = line.slice(6).trim();
    else if (line.startsWith("data:")) data.push(line.slice(5).trimStart());
  }
  if (!kind || !data.length) return null;
  try {
    const payload = JSON.parse(data.join("\n"));
    if (kind === "done") return { type: "done", result: payload as AskResponse };
    return { type: kind, ...payload } as StreamEvent;
  } catch {
    return null;
  }
}

export function uploadDocument(file: File): Promise<UploadResponse> {
  const form = new FormData();
  form.append("file", file);
  // No Content-Type: the browser sets the multipart boundary itself.
  return request<UploadResponse>("/documents/upload", { method: "POST", body: form }, false);
}

export interface RetrieveOptions {
  query: string;
  persona: string;
  limit?: number;
  strategies?: Strategy[];
  signal?: AbortSignal;
}

export function retrieve({
  query,
  persona,
  limit = 8,
  strategies = ["dense", "sparse", "hybrid", "reranked"],
  signal,
}: RetrieveOptions): Promise<RetrieveResponse> {
  return request<RetrieveResponse>("/retrieve", {
    method: "POST",
    body: JSON.stringify({ query, persona, limit, strategies }),
    signal,
  });
}

export function getEvaluation(): Promise<EvaluationResponse> {
  return request<EvaluationResponse>("/evaluation");
}

export function getDocuments(persona: string): Promise<DocumentsResponse> {
  // The persona goes to the server because the server does the filtering.
  // Fetching everything and hiding rows here would ship the titles of
  // documents a guest may not read to a guest.
  return request<DocumentsResponse>(`/documents?persona=${encodeURIComponent(persona)}`);
}
