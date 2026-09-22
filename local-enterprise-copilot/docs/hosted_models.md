# Running on hosted models instead of Ollama

The project was built to run entirely on a local machine: `llama3.1:8b` and
`qwen3-embedding:0.6b` through Ollama, with nothing sent anywhere. That is
still the default and still works.

It does not survive containerisation. The deployment container has no GPU, so
there is no local model for it to call. This document covers the alternative:
**Groq for chat, Cloudflare Workers AI for embeddings.**

---

## 1. What actually changes

| | Local (default) | Hosted |
|---|---|---|
| Routing, SQL generation, answers | `llama3.1:8b`, local | Groq |
| Embeddings | `qwen3-embedding:0.6b`, local | Cloudflare `@cf/baai/bge-m3` |
| Prompts, including retrieved passages and the schema | stay put | **sent to the provider** |
| Query results (customer rows) | stay put | **stay put** |
| SQL guard, tenant isolation, citation validation | unchanged | **unchanged** |

The last row is the important one. A hosted model is exactly as untrusted as a
local one — the system never trusted the model's output in the first place.
Generated SQL still goes through `SQLGuard` (sqlglot AST validation, schema
allow-list, blocked columns, join ceiling, tenant predicate) and then
`ReadOnlyRunner`. Retrieved text is still never treated as instructions.
Citations are still checked against the evidence package.

What changes is disclosure, not safety. **`README.md` and `docs/security.md`
say nothing leaves this machine. That is false once this is configured**, and
those documents need updating alongside it. `/health` reports the posture as a
`data_locality` check so it is visible without reading the container's
environment.

---

## 2. Chat: Groq

Groq speaks the OpenAI protocol, so `LLM_PROVIDER=openai` with Groq's base URL
is all it takes.

1. Sign up at **https://console.groq.com** and create an API key. It starts
   with `gsk_`.
2. Configure:

```ini
LLM_PROVIDER=openai
LLM_BASE_URL=https://api.groq.com/openai/v1
LLM_API_KEY=gsk_your_key_here
LLM_MODEL=llama-3.3-70b-versatile
```

`LLM_MODEL` is required, and the factory refuses to start without it. The
model profile's name is an Ollama tag — sending `llama3.1:8b` to Groq produces
a 404 that reads like a network problem.

### Which model

| Model | Context | Notes |
|---|---|---|
| `llama-3.3-70b-versatile` | 131k | Strongest general model. The default recommendation. |
| `openai/gpt-oss-120b` | 131k | Strong reasoning. Worth comparing on SQL. |
| `llama-3.1-8b-instant` | 131k | **The one to evaluate with first.** |

That last row matters for a reason that is easy to miss. The published
evaluation numbers were measured with a local `llama3.1:8b`. If you switch
straight to a 70B, any change in quality confounds two variables — hosted
versus local, and bigger versus smaller — and you cannot say which caused it.
Running `llama-3.1-8b-instant` first isolates the transport change.

### One quirk

Groq rejects `temperature = 0` and substitutes `1e-8`. This project asks for
`0.0` everywhere it wants determinism, so the client sends `1e-8` itself
rather than relying on an undocumented coercion — and so a provider that does
*not* coerce returns a visible error instead of silently sampling.

---

## 3. Embeddings: Cloudflare Workers AI

Groq has no embeddings endpoint. Neither does Anthropic. A hosted chat model
almost always means embeddings from somewhere else.

`@cf/baai/bge-m3` is the right fit here for three reasons:

- **1024 dimensions** — the same as the local `qwen3-embedding:0.6b` the index
  was built with, so the Qdrant collection's vector size does not change.
- **Multilingual, 100+ languages.** The evaluation has a `multilingual`
  category where sparse retrieval collapses to 0.333 and dense carries the
  result; a multilingual embedder is the part of the stack that matters there.
- **Free tier**, 10,000 neurons/day shared across models, and the same
  Cloudflare account the rest of the deployment already uses.

The corpus is 19 documents and 130 chunks. Building the index is one small
job; after that it is one embedding per question.

### Setup

1. Cloudflare dashboard → **Workers & Pages → Workers AI**, and note your
   **Account ID**.
2. **My Profile → API Tokens → Create Token**, with the **Workers AI: Read**
   permission.
3. Configure:

```ini
EMBEDDING_PROVIDER=cloudflare
EMBEDDING_MODEL=@cf/baai/bge-m3
EMBEDDING_CLOUDFLARE_ACCOUNT_ID=your_account_id
EMBEDDING_CLOUDFLARE_API_TOKEN=your_token
```

### You must rebuild the index

This is not optional and it fails silently if skipped. A different model
produces different vectors for the same text, so the existing collection does
not become stale — it becomes **meaningless**, and meaningless vectors still
return a confident top-8.

```powershell
# Bump the version first, so a stale index can never be queried by a newer
# configuration. QDRANT_INDEX_VERSION exists for exactly this.
#   QDRANT_INDEX_VERSION=v2
.venv\Scripts\python scripts\build_index.py --rebuild
```

Then re-run the evaluation. The numbers in `docs/evaluation_report.md` were
measured with a different embedder and no longer describe the system:

```powershell
.venv\Scripts\python scripts\evaluate_retrieval.py --k 8
```

```powershell
.venv\Scripts\python scripts\evaluate_rigorous.py
```

---

## 4. Secrets

Neither key belongs in a file. Locally they can live in `.env`, which is
gitignored. In deployment they are platform secrets:

```bash
fly secrets set LLM_API_KEY="gsk_..." EMBEDDING_CLOUDFLARE_API_TOKEN="..."
```

Both are `SecretStr`, so logging or dumping the settings object prints
`**********`. `describe_providers()` is the sanctioned way to report
configuration — it has no branch that can emit a key, and a test asserts it.

A blank `LLM_API_KEY=` means *absent*, not *empty string*. That distinction is
enforced by a validator, because the same mistake with `MSSQL_PASSWORD`
produced a real defect: an empty `SecretStr` passed an `is not None` check and
reached the driver.

---

## 5. Other providers

`LLM_PROVIDER=openai` is not OpenAI-specific — it is the OpenAI *protocol*.
Change `LLM_BASE_URL` and it points anywhere that speaks it:

| Provider | Base URL |
|---|---|
| Groq | `https://api.groq.com/openai/v1` |
| OpenRouter | `https://openrouter.ai/api/v1` |
| OpenAI | `https://api.openai.com/v1` |
| Together | `https://api.together.xyz/v1` |
| DeepSeek | `https://api.deepseek.com/v1` |
| vLLM (self-hosted) | `http://your-host:8000/v1` |

`EMBEDDING_PROVIDER=openai` works the same way for any OpenAI-compatible
`/embeddings` endpoint.

To go back to fully local, set both providers to `ollama`. Nothing else needs
touching — except the index, if the embedder changed.
