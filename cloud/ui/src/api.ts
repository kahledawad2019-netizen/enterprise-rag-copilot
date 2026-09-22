import type {
  AskResponse,
  DocumentsResponse,
  EvaluationResponse,
  HealthResponse,
  MetaResponse,
  RetrieveResponse,
  Strategy,
} from "./types";

/**
 * All network access lives here.
 *
 * The base URL is relative on purpose. In production the UI and the Worker sit
 * behind the same hostname, so a relative path means no CORS preflight and no
 * build-time configuration to get wrong. In development Vite proxies /api to
 * `wrangler dev`, so the same code path runs in both.
 */
const BASE = "/api";

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

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${BASE}${path}`, {
      ...init,
      headers: { "Content-Type": "application/json", ...init?.headers },
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
