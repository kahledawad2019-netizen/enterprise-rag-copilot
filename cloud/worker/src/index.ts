/**
 * Cloudflare Worker - the API gateway in front of the copilot backend.
 *
 * Everything the browser is allowed to reach goes through here. The backend
 * container is never addressable from the public internet: its URL and its
 * service token live in this Worker's environment and are never serialised
 * into a response.
 *
 * Responsibilities, in the order a request meets them:
 *
 *   1. CORS        - an explicit origin allow-list, not a wildcard. A wildcard
 *                    with credentials is rejected by browsers anyway, and the
 *                    UI and the API share a domain in the normal deployment.
 *   2. Identity    - a Cloudflare Access JWT, verified against the team's
 *                    JWKS and cached per isolate. Authentication fails closed
 *                    outside an explicitly disabled local-development mode.
 *   3. Rate limit  - per identity when there is one, per IP otherwise.
 *   4. Proxy       - to the backend, adding the service token. Streaming
 *                    responses pass through untouched so the UI can render
 *                    tokens as they arrive.
 *
 * What this Worker deliberately does NOT do: it does not inspect or rewrite
 * generated SQL, and it does not decide what the model may read. Those are the
 * backend's guards (sqlglot AST validation, tenant injection, the read-only
 * principal). Duplicating half of a safety check in a second place is how the
 * two copies drift apart.
 */

export interface Env {
  /** Public origin(s) allowed to call this API. Comma-separated. */
  ALLOWED_ORIGINS: string;
  /** Base URL of the FastAPI container, e.g. https://copilot-api.fly.dev */
  BACKEND_URL: string;
  /** Shared secret presented to the backend. `wrangler secret put BACKEND_TOKEN` */
  BACKEND_TOKEN: string;

  /** Cloudflare Access application audience tag. Unset = auth disabled. */
  ACCESS_AUD?: string;
  /** e.g. myteam.cloudflareaccess.com */
  ACCESS_TEAM_DOMAIN?: string;
  /** Authentication provider: Cloudflare Access or Clerk. */
  AUTH_PROVIDER?: string;
  /** Public Clerk JWKS endpoint for the configured instance. */
  CLERK_JWKS_URL?: string;
  /** Exact Clerk issuer URL shown in the instance's API keys page. */
  CLERK_ISSUER?: string;
  /** Comma-separated UI origins accepted in the token's `azp` claim. */
  CLERK_AUTHORIZED_PARTIES?: string;
  /** Expected signed audience. */
  CLERK_AUDIENCE?: string;
  /** Required outside local development; disabled is fail-closed elsewhere. */
  AUTH_MODE?: string;
  ENVIRONMENT?: string;

  /** Native rate-limiting binding. Optional. */
  ASK_LIMITER?: RateLimit;
}

interface RateLimit {
  limit(options: { key: string }): Promise<{ success: boolean }>;
}

/** Routes the browser may call, and the backend path each maps to. */
const ROUTES: Record<string, { method: string; upstream: string; limited: boolean }> = {
  "GET /api/health": { method: "GET", upstream: "/health", limited: false },
  "GET /api/meta": { method: "GET", upstream: "/meta", limited: false },
  "POST /api/ask": { method: "POST", upstream: "/ask", limited: true },
  "GET /api/documents": { method: "GET", upstream: "/documents", limited: false },
  "POST /api/retrieve": { method: "POST", upstream: "/retrieve", limited: true },
  "GET /api/evaluation": { method: "GET", upstream: "/evaluation", limited: false },
  "GET /api/traces": { method: "GET", upstream: "/traces", limited: false },
};

const MAX_BODY_BYTES = 64 * 1024;
const UPSTREAM_TIMEOUT_MS = 120_000;

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);
    const origin = request.headers.get("Origin");
    const cors = corsHeaders(origin, env);

    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: cors });
    }

    const routeKey = `${request.method} ${url.pathname}`;
    const route = ROUTES[routeKey];
    if (!route) {
      return json({ error: "not_found", detail: `No route for ${routeKey}` }, 404, cors);
    }

    // ---- identity ----
    let identity: string | null = null;
    const authMode = (env.AUTH_MODE ?? "required").toLowerCase();
    const environment = (env.ENVIRONMENT ?? "production").toLowerCase();
    if (authMode === "disabled") {
      if (environment !== "development") {
        return json(
          { error: "auth_misconfigured", detail: "Authentication can be disabled only in development." },
          503,
          cors,
        );
      }
    } else if (authMode === "required") {
      const provider = (env.AUTH_PROVIDER ?? "access").toLowerCase();
      let verdict: Verdict;
      if (provider === "clerk") {
        if (
          !env.CLERK_JWKS_URL ||
          !env.CLERK_ISSUER ||
          !env.CLERK_AUTHORIZED_PARTIES ||
          !env.CLERK_AUDIENCE
        ) {
          return json(
            { error: "auth_misconfigured", detail: "Clerk authentication is required but not configured." },
            503,
            cors,
          );
        }
        verdict = await verifyClerkJwt(request, env);
      } else if (provider === "access") {
        if (!env.ACCESS_AUD || !env.ACCESS_TEAM_DOMAIN) {
          return json(
            { error: "auth_misconfigured", detail: "Cloudflare Access is required but not configured." },
            503,
            cors,
          );
        }
        verdict = await verifyAccessJwt(request, env);
      } else {
        return json({ error: "auth_misconfigured", detail: "AUTH_PROVIDER is invalid." }, 503, cors);
      }
      if (!verdict.ok) {
        return json({ error: "unauthorized", detail: verdict.reason }, 401, cors);
      }
      identity = verdict.email;
    } else {
      return json({ error: "auth_misconfigured", detail: "AUTH_MODE is invalid." }, 503, cors);
    }

    // ---- rate limit ----
    if (route.limited && env.ASK_LIMITER) {
      const subject = identity ?? request.headers.get("CF-Connecting-IP") ?? "anonymous";
      const { success } = await env.ASK_LIMITER.limit({ key: subject });
      if (!success) {
        return json(
          { error: "rate_limited", detail: "Too many questions. Try again shortly." },
          429,
          { ...cors, "Retry-After": "60" },
        );
      }
    }

    // ---- body, size-capped ----
    // A request body here is a question and a few flags. Anything larger is
    // either a mistake or an attempt to make the backend do unbounded work.
    let body: string | undefined;
    if (request.method !== "GET") {
      const cappedBody = await readBodyCapped(request, MAX_BODY_BYTES);
      if (cappedBody === null) {
        return json({ error: "payload_too_large" }, 413, cors);
      }
      body = cappedBody;
    }

    // ---- proxy ----
    const upstream = new URL(route.upstream, env.BACKEND_URL);
    upstream.search = url.search;

    const headers = new Headers({
      "Content-Type": "application/json",
      Authorization: `Bearer ${env.BACKEND_TOKEN}`,
      "X-Request-Id": crypto.randomUUID(),
    });
    if (identity) headers.set("X-Copilot-User", identity);

    let upstreamResponse: Response;
    try {
      upstreamResponse = await fetch(upstream.toString(), {
        method: route.method,
        headers,
        body,
        signal: AbortSignal.timeout(UPSTREAM_TIMEOUT_MS),
      });
    } catch (err) {
      // The backend is a container that can cold-start or be mid-redeploy.
      // Saying which of the two it looks like is more useful than a bare 502,
      // and neither branch leaks the backend URL.
      const reason = err instanceof Error ? err.name : "unknown";
      return json(
        {
          error: "backend_unavailable",
          detail:
            reason === "TimeoutError"
              ? "The copilot did not answer in time. Generation is slow on the first request after a cold start."
              : "The copilot backend is not reachable.",
        },
        503,
        cors,
      );
    }

    // Stream the body through untouched. Buffering would defeat token-by-token
    // rendering in the UI and raise this Worker's memory ceiling for no gain.
    const responseHeaders = new Headers(cors);
    for (const pass of ["Content-Type", "Cache-Control", "X-Trace-Id"]) {
      const value = upstreamResponse.headers.get(pass);
      if (value) responseHeaders.set(pass, value);
    }
    responseHeaders.set("X-Content-Type-Options", "nosniff");
    responseHeaders.set("Referrer-Policy", "no-referrer");

    return new Response(upstreamResponse.body, {
      status: upstreamResponse.status,
      headers: responseHeaders,
    });
  },
} satisfies ExportedHandler<Env>;

// ---------------------------------------------------------------------------
// CORS
// ---------------------------------------------------------------------------

function corsHeaders(origin: string | null, env: Env): Record<string, string> {
  const allowed = (env.ALLOWED_ORIGINS ?? "")
    .split(",")
    .map((o) => o.trim())
    .filter(Boolean);

  const headers: Record<string, string> = {
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "Authorization, Content-Type, CF-Access-Jwt-Assertion",
    "Access-Control-Max-Age": "86400",
    Vary: "Origin",
  };

  if (origin && allowed.includes(origin)) {
    headers["Access-Control-Allow-Origin"] = origin;
    headers["Access-Control-Allow-Credentials"] = "true";
  }
  return headers;
}

function json(payload: unknown, status: number, headers: Record<string, string>): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { ...headers, "Content-Type": "application/json" },
  });
}

async function readBodyCapped(request: Request, maximum: number): Promise<string | null> {
  const declared = Number(request.headers.get("Content-Length"));
  if (Number.isFinite(declared) && declared > maximum) return null;
  if (!request.body) return "";

  const reader = request.body.getReader();
  const chunks: Uint8Array[] = [];
  let total = 0;
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    total += value.byteLength;
    if (total > maximum) {
      await reader.cancel("request body exceeds the configured limit");
      return null;
    }
    chunks.push(value);
  }

  const joined = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    joined.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return new TextDecoder().decode(joined);
}

// ---------------------------------------------------------------------------
// Cloudflare Access JWT verification
//
// Access puts a signed JWT on every request that passes its policy. Trusting
// the CF-Access-Authenticated-User-Email header alone is not enough: a request
// that reaches the Worker without going through Access can set any header it
// likes. The signature is what makes it an identity.
// ---------------------------------------------------------------------------

interface Jwk {
  kid: string;
  kty: string;
  alg: string;
  n: string;
  e: string;
}

let jwksCache: { teamDomain: string; keys: Map<string, CryptoKey>; fetchedAt: number } | null = null;
const JWKS_TTL_MS = 60 * 60 * 1000;

async function getSigningKeys(teamDomain: string): Promise<Map<string, CryptoKey>> {
  if (
    jwksCache &&
    jwksCache.teamDomain === teamDomain &&
    Date.now() - jwksCache.fetchedAt < JWKS_TTL_MS
  ) {
    return jwksCache.keys;
  }
  const response = await fetch(`https://${teamDomain}/cdn-cgi/access/certs`);
  if (!response.ok) throw new Error(`JWKS fetch failed: ${response.status}`);

  const { keys } = (await response.json()) as { keys: Jwk[] };
  const imported = new Map<string, CryptoKey>();
  for (const jwk of keys) {
    imported.set(
      jwk.kid,
      await crypto.subtle.importKey(
        "jwk",
        { kty: jwk.kty, n: jwk.n, e: jwk.e, alg: "RS256", ext: true },
        { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" },
        false,
        ["verify"],
      ),
    );
  }
  jwksCache = { teamDomain, keys: imported, fetchedAt: Date.now() };
  return imported;
}

type Verdict = { ok: true; email: string } | { ok: false; reason: string };

async function verifyClerkJwt(request: Request, env: Env): Promise<Verdict> {
  const authorization = request.headers.get("Authorization");
  const token = authorization?.match(/^Bearer\s+(.+)$/i)?.[1];
  if (!token) return { ok: false, reason: "no Clerk session token on the request" };

  const authorizedParties = (env.CLERK_AUTHORIZED_PARTIES ?? "")
    .split(",")
    .map((party) => party.trim().replace(/\/+$/, ""))
    .filter(Boolean);

  const [headerB64, payloadB64, signatureB64] = token.split(".");
  if (!headerB64 || !payloadB64 || !signatureB64) {
    return { ok: false, reason: "malformed Clerk token" };
  }

  let header: { kid?: string; alg?: string; typ?: string };
  let claims: {
    aud?: string | string[];
    azp?: string;
    email?: string;
    exp?: number;
    iss?: string;
    nbf?: number;
    sts?: string;
    sub?: string;
    act?: unknown;
  };
  try {
    header = JSON.parse(decodeB64Url(headerB64)) as typeof header;
    claims = JSON.parse(decodeB64Url(payloadB64)) as typeof claims;
  } catch {
    return { ok: false, reason: "malformed Clerk token claims" };
  }
  if (header.alg !== "RS256") return { ok: false, reason: "unexpected Clerk signing algorithm" };
  if (header.typ && header.typ !== "JWT") return { ok: false, reason: "unexpected Clerk token type" };

  let keys: Map<string, CryptoKey>;
  try {
    keys = await getClerkSigningKeys(env.CLERK_JWKS_URL as string);
  } catch {
    return { ok: false, reason: "could not fetch Clerk signing keys" };
  }
  const key = header.kid ? keys.get(header.kid) : undefined;
  if (!key) return { ok: false, reason: "unknown Clerk signing key" };

  let signatureValid = false;
  try {
    signatureValid = await crypto.subtle.verify(
      "RSASSA-PKCS1-v1_5",
      key,
      b64UrlToBytes(signatureB64),
      new TextEncoder().encode(`${headerB64}.${payloadB64}`),
    );
  } catch {
    return { ok: false, reason: "malformed Clerk token signature" };
  }
  if (!signatureValid) return { ok: false, reason: "bad Clerk token signature" };

  const now = Date.now();
  if (!claims.exp || claims.exp * 1000 < now) {
    return { ok: false, reason: "Clerk token expired" };
  }
  if (claims.nbf && claims.nbf * 1000 > now + 5_000) {
    return { ok: false, reason: "Clerk token not active" };
  }
  const audiences = Array.isArray(claims.aud) ? claims.aud : [claims.aud];
  if (!audiences.includes(env.CLERK_AUDIENCE)) {
    return { ok: false, reason: "Clerk token was issued for a different application" };
  }
  if (!claims.azp || !authorizedParties.includes(claims.azp.replace(/\/+$/, ""))) {
    return { ok: false, reason: "Clerk token came from an unauthorized frontend" };
  }
  const issuer = String(claims.iss ?? "").replace(/\/+$/, "");
  if (issuer !== env.CLERK_ISSUER?.replace(/\/+$/, "")) {
    return { ok: false, reason: "token was issued by a different Clerk instance" };
  }
  if (claims.sts === "pending") {
    return { ok: false, reason: "Clerk session is not active" };
  }
  if (claims.act) {
    return { ok: false, reason: "impersonated Clerk sessions are not allowed" };
  }
  const email = typeof claims.email === "string" ? claims.email.trim().toLowerCase() : "";
  if (!email || !claims.sub) {
    return { ok: false, reason: "token has no verified user identity" };
  }
  return { ok: true, email };
}

let clerkJwksCache: { url: string; keys: Map<string, CryptoKey>; fetchedAt: number } | null = null;

async function getClerkSigningKeys(url: string): Promise<Map<string, CryptoKey>> {
  if (
    clerkJwksCache &&
    clerkJwksCache.url === url &&
    Date.now() - clerkJwksCache.fetchedAt < JWKS_TTL_MS
  ) {
    return clerkJwksCache.keys;
  }

  const parsed = new URL(url);
  if (parsed.protocol !== "https:" || !parsed.hostname.endsWith(".clerk.accounts.dev")) {
    throw new Error("untrusted Clerk JWKS URL");
  }
  const response = await fetch(parsed.toString());
  if (!response.ok) throw new Error(`Clerk JWKS fetch failed: ${response.status}`);

  const { keys } = (await response.json()) as { keys: Jwk[] };
  const imported = new Map<string, CryptoKey>();
  for (const jwk of keys) {
    if (jwk.alg !== "RS256" || jwk.kty !== "RSA") continue;
    imported.set(
      jwk.kid,
      await crypto.subtle.importKey(
        "jwk",
        { kty: jwk.kty, n: jwk.n, e: jwk.e, alg: "RS256", ext: true },
        { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" },
        false,
        ["verify"],
      ),
    );
  }
  if (!imported.size) throw new Error("Clerk JWKS contains no supported signing keys");
  clerkJwksCache = { url: parsed.toString(), keys: imported, fetchedAt: Date.now() };
  return imported;
}

async function verifyAccessJwt(request: Request, env: Env): Promise<Verdict> {
  const token =
    request.headers.get("CF-Access-Jwt-Assertion") ?? readCookie(request, "CF_Authorization");

  if (!token) return { ok: false, reason: "no Access token on the request" };

  const [headerB64, payloadB64, signatureB64] = token.split(".");
  if (!headerB64 || !payloadB64 || !signatureB64) {
    return { ok: false, reason: "malformed token" };
  }

  let keys: Map<string, CryptoKey>;
  try {
    keys = await getSigningKeys(env.ACCESS_TEAM_DOMAIN as string);
  } catch {
    return { ok: false, reason: "could not fetch Access signing keys" };
  }

  let header: { kid?: string; alg?: string };
  let claims: {
    aud?: string | string[];
    exp?: number;
    nbf?: number;
    iss?: string;
    email?: string;
  };
  try {
    header = JSON.parse(decodeB64Url(headerB64)) as { kid?: string; alg?: string };
    claims = JSON.parse(decodeB64Url(payloadB64)) as typeof claims;
  } catch {
    return { ok: false, reason: "malformed token claims" };
  }
  if (header.alg !== "RS256") return { ok: false, reason: "unexpected signing algorithm" };
  const key = header.kid ? keys.get(header.kid) : undefined;
  if (!key) return { ok: false, reason: "unknown signing key" };

  let verified = false;
  try {
    verified = await crypto.subtle.verify(
      "RSASSA-PKCS1-v1_5",
      key,
      b64UrlToBytes(signatureB64),
      new TextEncoder().encode(`${headerB64}.${payloadB64}`),
    );
  } catch {
    return { ok: false, reason: "malformed token signature" };
  }
  if (!verified) return { ok: false, reason: "bad signature" };

  const audiences = Array.isArray(claims.aud) ? claims.aud : [claims.aud];
  if (!audiences.includes(env.ACCESS_AUD)) {
    return { ok: false, reason: "token was issued for a different application" };
  }
  if (!claims.exp || claims.exp * 1000 < Date.now()) {
    return { ok: false, reason: "token expired" };
  }
  if (claims.nbf && claims.nbf * 1000 > Date.now()) {
    return { ok: false, reason: "token not active" };
  }
  const expectedIssuer = `https://${env.ACCESS_TEAM_DOMAIN}`.replace(/\/+$/, "");
  if ((claims.iss ?? "").replace(/\/+$/, "") !== expectedIssuer) {
    return { ok: false, reason: "token was issued by a different Access team" };
  }
  if (!claims.email) return { ok: false, reason: "token has no email identity" };

  return { ok: true, email: claims.email.toLowerCase() };
}

function readCookie(request: Request, name: string): string | null {
  const header = request.headers.get("Cookie");
  if (!header) return null;
  for (const part of header.split(";")) {
    const [k, ...rest] = part.trim().split("=");
    if (k === name) return rest.join("=");
  }
  return null;
}

function b64UrlToBytes(input: string): Uint8Array {
  const b64 = input.replace(/-/g, "+").replace(/_/g, "/");
  const padded = b64.padEnd(b64.length + ((4 - (b64.length % 4)) % 4), "=");
  const binary = atob(padded);
  return Uint8Array.from(binary, (c) => c.charCodeAt(0));
}

function decodeB64Url(input: string): string {
  return new TextDecoder().decode(b64UrlToBytes(input));
}
