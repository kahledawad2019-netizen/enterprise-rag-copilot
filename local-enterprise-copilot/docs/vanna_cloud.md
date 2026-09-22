# Vanna Cloud

> **Experimental / no production deployment.** The public Vanna repository
> was archived on 29 March 2026, and the integration below uses the legacy
> `vanna.legacy` remote API rather than the V2 Agent API. Vanna's current
> security documentation describes its premium backend as development/demo
> software and does not publish a firm retention period. The production image
> therefore excludes Vanna; staging uses `TEXT_TO_SQL_PROVIDER=native` until a
> live account smoke test, dependency review, data-processing terms and
> retention/deletion guarantees have been approved.

Read this before setting `VANNA_API_KEY`. Enabling Vanna Cloud is the only
change in this project that sends data to a third party, and it contradicts
claims made elsewhere in this repository unless those are updated with it.

---

## 1. What changes

The README and `docs/security.md` say the system runs entirely locally.
With `TEXT_TO_SQL_PROVIDER=vanna_cloud` that stops being true for the
Text-to-SQL path. Document retrieval, answer generation, the SQL guard and
query execution are untouched and stay local.

| | `hybrid` | `cloud` |
|---|---|---|
| Table and column names (DDL) | uploaded once | uploaded once |
| Business glossary | uploaded once | uploaded once |
| Approved SQL examples | uploaded once | uploaded once |
| The user's question | sent per query | sent per query |
| The assembled prompt | stays local | sent per query |
| SQL generation | local model | Vanna's hosted model |
| **Query results (customer rows)** | **never sent** | **never sent** |

Results are never sent in either mode. Vanna supports feeding them back into
the prompt to refine a follow-up; `VANNA_ALLOW_LLM_TO_SEE_DATA` controls it,
defaults to false, and should stay false. That flag is the difference between
sending a schema and sending customer data.

Do not select either mode in the production container. If you run an isolated
legacy smoke test, `cloud` avoids the Ollama dependency while `hybrid` calls a
local Ollama instance.

### What does not change

Vanna produces a **string**. That string still goes through `SQLGuard`
(sqlglot AST validation, schema allow-list, blocked columns, join ceiling,
tenant predicate) and then `ReadOnlyRunner`, exactly as it does for the local
providers. No safety property depends on Vanna behaving correctly, and none
of them move to the cloud. See `docs/text_to_sql.md`.

---

## 2. Getting a key

1. Go to **https://vanna.ai** and sign up. There is a free tier.
2. Open the dashboard and copy your **API key**.
3. Create a model — Vanna's word for a training corpus, not a language model.
   Name it `enterprise-copilot` to match the default, or pick your own and set
   `VANNA_MODEL` to match.

That is the whole signup. The key is the only credential; there is no separate
project id or secret.

## 3. Configuring it

**Local development** — add to `.env` (which is gitignored):

```ini
TEXT_TO_SQL_PROVIDER=vanna_cloud
VANNA_API_KEY=vn-your-key-here
VANNA_MODEL=enterprise-copilot
VANNA_MODE=hybrid
```

**In an isolated experimental container** — never in a file. Install
`cloud/api/requirements-vanna-experimental.txt`, then set the key as a platform
secret:

```bash
fly secrets set VANNA_API_KEY=vn-your-key-here VANNA_MODE=cloud
```

The setting is a pydantic `SecretStr`, so logging or dumping the settings
object prints `**********`. The only place it is unwrapped is where the client
is constructed.

A blank `VANNA_API_KEY=` means *absent*, not *empty string*. That distinction
is enforced by a validator, because the same mistake with `MSSQL_PASSWORD`
already produced one real defect in this project: an empty `SecretStr` passed
an `is not None` check and was sent to the driver.

## 4. First run

```powershell
.venv\Scripts\python scripts\query.py --provider vanna_cloud "Which five customers have the highest ARR?"
```

On the first call the provider uploads the corpus: one DDL statement per table,
one documentation entry per glossary term and per foreign-key relationship, and
one question/SQL pair per approved example.

On later calls it checks the remote row count first and skips the upload. This
matters more than it does locally: the cloud corpus is durable and shared, so
every container start would otherwise add another copy of all 26 DDL
statements. Duplicated training data degrades retrieval.

After a schema change, force a re-upload:

```python
provider.ensure_trained(force=True)
```

## 5. If it is not configured

`build_provider` falls back to the native provider and logs why. The two
failure modes are reported separately on purpose:

```
Vanna Cloud is not configured (VANNA_API_KEY is not set...); using the native provider.
Vanna is unavailable (No module named 'vanna'); using the native provider.
```

They need different fixes, and one generic warning sends people to the wrong
one.

## 6. Before you enable it in production

- [ ] Confirm sending your **real** schema and glossary to a third party is
      acceptable. Table and column names leak business structure; a glossary
      entry can leak more than the schema does.
- [ ] Confirm `VANNA_ALLOW_LLM_TO_SEE_DATA=false`.
- [ ] Update `README.md` and `docs/security.md`. Leaving "nothing leaves this
      machine" in place while this is enabled is worse than the disclosure
      itself.
- [ ] Re-run `scripts/evaluate_text_to_sql.py --provider vanna_cloud` and
      compare against the native and local-vanna numbers in
      `docs/evaluation_report.md`. A different model is a different accuracy
      profile, and the published figures do not describe it.
