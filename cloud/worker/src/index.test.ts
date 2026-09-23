import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";

import worker, { type Env } from "./index";

const ISSUER = "https://test-instance.clerk.accounts.dev";
const JWKS_URL = `${ISSUER}/.well-known/jwks.json`;
const AUDIENCE = "enterprise-rag-copilot-api";
const ORIGIN = "http://localhost:5173";

let privateKey: CryptoKey;
let publicJwk: JsonWebKey;

beforeAll(async () => {
  const pair = (await crypto.subtle.generateKey(
    {
      name: "RSASSA-PKCS1-v1_5",
      modulusLength: 2048,
      publicExponent: new Uint8Array([1, 0, 1]),
      hash: "SHA-256",
    },
    true,
    ["sign", "verify"],
  )) as CryptoKeyPair;
  privateKey = pair.privateKey;
  publicJwk = (await crypto.subtle.exportKey("jwk", pair.publicKey)) as JsonWebKey;
});

afterEach(() => vi.restoreAllMocks());

function env(overrides: Partial<Env> = {}): Env {
  return {
    ALLOWED_ORIGINS: ORIGIN,
    BACKEND_URL: "https://backend.example",
    BACKEND_TOKEN: "backend-service-token",
    AUTH_MODE: "required",
    AUTH_PROVIDER: "clerk",
    ENVIRONMENT: "staging",
    CLERK_AUDIENCE: AUDIENCE,
    CLERK_AUTHORIZED_PARTIES: ORIGIN,
    CLERK_ISSUER: ISSUER,
    CLERK_JWKS_URL: JWKS_URL,
    ...overrides,
  };
}

function request(token?: string): Request {
  const headers: Record<string, string> = { Origin: ORIGIN };
  if (token) headers.Authorization = `Bearer ${token}`;
  return new Request("https://worker.example/api/meta", { headers });
}

function encode(value: unknown): string {
  const bytes = new TextEncoder().encode(JSON.stringify(value));
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

async function token(overrides: Record<string, unknown> = {}, key = privateKey): Promise<string> {
  const now = Math.floor(Date.now() / 1000);
  const header = encode({ alg: "RS256", kid: "test-key", typ: "JWT" });
  const payload = encode({
    aud: AUDIENCE,
    azp: ORIGIN,
    email: "admin@example.com",
    exp: now + 60,
    iss: ISSUER,
    nbf: now - 1,
    sub: "user_test",
    ...overrides,
  });
  const signature = new Uint8Array(
    await crypto.subtle.sign(
      "RSASSA-PKCS1-v1_5",
      key,
      new TextEncoder().encode(`${header}.${payload}`),
    ),
  );
  let binary = "";
  for (const byte of signature) binary += String.fromCharCode(byte);
  const encodedSignature = btoa(binary)
    .replace(/\+/g, "-")
    .replace(/\//g, "_")
    .replace(/=+$/, "");
  return `${header}.${payload}.${encodedSignature}`;
}

function mockNetwork(): { upstream: Request[] } {
  const upstream: Request[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
      if (url === JWKS_URL) {
        return Response.json({
          keys: [{ ...publicJwk, alg: "RS256", kid: "test-key", kty: "RSA" }],
        });
      }
      const proxied = new Request(input, init);
      upstream.push(proxied);
      return Response.json({ ok: true });
    }),
  );
  return { upstream };
}

describe("Clerk authentication", () => {
  it("fails closed when Clerk configuration is missing", async () => {
    const response = await worker.fetch(request(), env({ CLERK_JWKS_URL: undefined }));
    expect(response.status).toBe(503);
  });

  it("rejects a missing token", async () => {
    const response = await worker.fetch(request(), env());
    expect(response.status).toBe(401);
  });

  it("rejects a malformed token", async () => {
    const response = await worker.fetch(request("not-a-jwt"), env());
    expect(response.status).toBe(401);
  });

  it.each([
    ["wrong audience", { aud: "another-api" }],
    ["wrong authorized party", { azp: "https://evil.example" }],
    ["expired token", { exp: 1 }],
    ["pending session", { sts: "pending" }],
    ["impersonated session", { act: { sub: "user_admin" } }],
  ])("rejects %s", async (_name, claims) => {
    mockNetwork();
    const response = await worker.fetch(request(await token(claims)), env());
    expect(response.status).toBe(401);
  });

  it("rejects a bad signature", async () => {
    const otherPair = (await crypto.subtle.generateKey(
      {
        name: "RSASSA-PKCS1-v1_5",
        modulusLength: 2048,
        publicExponent: new Uint8Array([1, 0, 1]),
        hash: "SHA-256",
      },
      true,
      ["sign", "verify"],
    )) as CryptoKeyPair;
    mockNetwork();
    const response = await worker.fetch(request(await token({}, otherPair.privateKey)), env());
    expect(response.status).toBe(401);
  });

  it("proxies a valid identity using only the backend service token", async () => {
    const { upstream } = mockNetwork();
    const response = await worker.fetch(request(await token()), env());

    expect(response.status).toBe(200);
    expect(upstream).toHaveLength(1);
    expect(upstream[0].headers.get("Authorization")).toBe("Bearer backend-service-token");
    expect(upstream[0].headers.get("X-Copilot-User")).toBe("admin@example.com");
  });
});
