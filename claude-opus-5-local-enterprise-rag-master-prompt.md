# Claude Opus 5 Master Prompt: Local Enterprise Intelligence Copilot

Copy this prompt into Cursor Agent using Claude Opus 5.

---

You are the lead AI engineer, data architect, ML engineer, security engineer, technical writer, and teacher for this project.

Your task is to design and implement a complete, professional, portfolio-grade Local Enterprise RAG and Analytics Copilot from A to Z.

Do not build a toy “chat with PDF” demo. Build a realistic enterprise project that demonstrates the skills expected from an AI/ML engineer working on production RAG systems.

## 1. Project identity

Project name:

**Local Enterprise Intelligence Copilot**

Business simulation:

Create a realistic fictional B2B SaaS company that sells subscription-based software products to enterprise customers.

The assistant must answer questions using two different knowledge sources:

1. Unstructured company documents:
   - Product documentation
   - Pricing policies
   - Refund policies
   - Customer onboarding guides
   - Support procedures
   - Service-level agreements
   - Security policies
   - Incident reports
   - Sales policies
   - KPI and business-definition documents

2. Structured Microsoft SQL Server data:
   - Customers
   - Products
   - Plans
   - Subscriptions
   - Invoices
   - Payments
   - Refunds
   - Product usage
   - Support tickets
   - SLA events
   - Incidents
   - Customer health and churn signals

The system must support questions such as:

- “What is the refund policy for enterprise annual plans?”
- “Which five customers have the highest ARR?”
- “Why did churn increase in Q2?”
- “Show customers with more than three SLA breaches and summarize the SLA policy.”
- “Compare the contractual response time with the actual response time for customer X.”
- “What does the company mean by an active customer?”
- “Calculate MRR using the official business definition.”
- “Which customers are at risk according to usage, unpaid invoices, and support tickets?”
- “What caused the June service incident, and which customers were affected?”

The project must demonstrate document RAG, Text-to-SQL, multi-source reasoning, tool routing, evaluation, observability, security, and local inference.

## 2. Development environment

The development machine uses:

- Windows
- Cursor IDE
- Claude Opus 5 as the coding assistant
- Microsoft SQL Server Management Studio
- Python
- Ollama for local runtime LLM inference
- Streamlit for the user interface

Important distinction:

- Claude Opus 5 is used only as the development and coding assistant inside Cursor.
- The final application must run using a local LLM through Ollama.
- The application must not require Claude, OpenAI, or another paid cloud LLM at runtime.
- Internet access may be required initially to install packages and download local models, but the completed application should be capable of running locally after installation.

Before implementation, inspect the environment and determine:

- Python version
- Available RAM
- CPU
- GPU model and VRAM, if available
- Installed Ollama version
- Currently downloaded Ollama models
- Whether Docker is available
- Whether a SQL Server Database Engine instance is installed
- Available SQL Server instances
- Whether Windows Authentication or SQL Authentication will be used
- Installed Microsoft ODBC Driver for SQL Server

Do not assume that installing SSMS means the SQL Server Database Engine is installed.

If the hardware cannot run the preferred model, define three configuration profiles:

- Lite profile for CPU or low-memory hardware
- Standard profile for moderate GPU/RAM
- High-quality profile for stronger hardware

Keep all model names configurable through environment variables.

## 3. Technical architecture

Build the system using modular services and explicit interfaces.

The target logical architecture is:

```text
User
→ Streamlit UI
→ Authentication/User Context
→ Query Understanding
→ Intent Router
→ Document RAG, Text-to-SQL, or Multi-Source workflow
→ Evidence Builder
→ Local Ollama LLM
→ Citation and Safety Validation
→ Final Answer
```

Offline document pipeline:

```text
Documents
→ Parsing/OCR
→ Cleaning
→ Structure-aware Chunking
→ Metadata Enrichment
→ Dense Embeddings
→ Sparse/BM25 Index
→ Qdrant
→ Index Versioning and Validation
```

Structured-data pipeline:

```text
User Question
→ Retrieve Relevant Schema
→ Retrieve Business Definitions
→ Retrieve Approved SQL Examples
→ Generate T-SQL
→ Parse and Validate SQL
→ Enforce Security Rules
→ Execute Read-only Query
→ Validate Results
→ Generate Explanation and Visualization
```

Observability pipeline:

```text
Request
→ Trace ID
→ Spans for every RAG and SQL stage
→ Structured Logs
→ Metrics
→ Evaluation Records
→ Debugging Dashboard
```

Avoid tightly coupling the project to a single orchestration framework. Core retrieval, reranking, routing, context construction, and evaluation logic must remain understandable and testable.

## 4. Required technology stack

Use:

- Python 3.11 or a compatible stable version
- Streamlit
- Ollama
- Microsoft SQL Server
- SQLAlchemy
- pyodbc
- Qdrant as the primary vector database
- Qdrant local/path mode as a lightweight fallback
- Dense local embeddings through Ollama
- Sparse retrieval or BM25
- Reciprocal Rank Fusion or another documented rank-fusion method
- A local multilingual reranker
- Pydantic for configuration and structured outputs
- sqlglot or another reliable SQL parser for SQL validation
- pytest
- OpenTelemetry-compatible tracing
- A local observability option such as Phoenix
- Structured JSON logging
- Pandas and Plotly for analytics output
- Faker or equivalent tooling for synthetic data generation

Preferred model strategy:

- Use a configurable Ollama chat/instruction model with reliable tool calling and structured output.
- Use a separate local embedding model.
- Start by evaluating `qwen3-embedding` or another strong multilingual embedding model supported by Ollama.
- Evaluate a multilingual local reranker such as a BGE reranker.
- Do not hard-code model names throughout the source code.
- Store model names and inference settings in a central configuration file and environment variables.
- Use the same embedding model and compatible embedding instructions for indexing and querying.
- Detect the embedding dimension instead of assuming it.

Do not use a cloud API as an undocumented fallback.

## 5. Vanna AI integration

Vanna AI must be included as the default Text-to-SQL agent integration.

However:

- The current Vanna ecosystem has changed significantly between versions.
- Do not mix legacy Vanna 0.x examples with Vanna 2.x APIs.
- Inspect the actually installed Vanna version.
- Consult its current official API before implementing.
- Pin the exact compatible package version.
- Add an Architecture Decision Record explaining the selected Vanna version.
- Document the maintenance risk caused by the official repository’s archived status.

Isolate Vanna behind an application-owned interface such as:

```text
TextToSQLProvider
- generate_query(...)
- validate_query(...)
- execute_query(...)
- explain_result(...)
- get_trace_metadata(...)
```

Implement:

- `VannaTextToSQLProvider` as the default provider
- `NativeTextToSQLProvider` as a maintainable fallback or reference implementation

The native provider must use the same schema retrieval, business glossary, approved-query examples, SQL safety validator, and database runner.

Vanna or the native Text-to-SQL workflow must retrieve:

- Relevant table schemas
- Column descriptions
- Table relationships
- Business definitions
- Approved question-to-SQL examples
- SQL Server dialect instructions
- User and tenant permissions

Do not send the entire database schema to the LLM for every question when schema retrieval can select the relevant subset.

## 6. SQL Server database

Use SQL Server as the main structured database.

Create version-controlled scripts:

- `sql/001_create_database.sql`
- `sql/002_create_schemas.sql`
- `sql/003_create_tables.sql`
- `sql/004_create_indexes.sql`
- `sql/005_create_views.sql`
- `sql/006_create_security.sql`
- `sql/007_seed_reference_data.sql`
- `sql/008_validation_queries.sql`

Use a database name such as `EnterpriseCopilot`.

Create appropriate database schemas such as:

- `core`
- `billing`
- `support`
- `analytics`
- `security`
- `ai`

Design normalized tables for:

- Tenants or business units
- Customers
- Customer contacts using synthetic data only
- Products
- Plans
- Subscriptions
- Subscription changes
- Invoices
- Invoice items
- Payments
- Refunds
- Usage events or daily usage aggregates
- Support tickets
- Ticket events
- SLA policies
- SLA breaches
- Incidents
- Incident-customer impact
- Customer health scores
- Business glossary
- Approved SQL examples
- AI audit events

Create safe analytics views such as:

- `analytics.vw_customer_360`
- `analytics.vw_monthly_recurring_revenue`
- `analytics.vw_churn_metrics`
- `analytics.vw_sla_performance`
- `analytics.vw_customer_risk`
- `analytics.vw_incident_impact`

The Text-to-SQL agent should prefer approved analytics views when possible.

Provide connection examples for:

- Windows Authentication
- SQL Authentication
- Local SQL Server Express or Developer Edition
- Configurable server and instance names
- ODBC Driver 18, with configuration for local certificate trust where necessary

Never commit real credentials.

## 7. Synthetic data

Generate realistic, deterministic synthetic data using a fixed random seed.

Create enough data to demonstrate realistic retrieval and analytics without making local setup unnecessarily heavy.

Include approximately:

- Multiple tenants or business units
- Hundreds or thousands of customers
- Several SaaS products and plans
- Two or three years of subscriptions and billing activity
- Usage data
- Support tickets
- SLA breaches
- Incidents
- Churn and expansion events

Deliberately include meaningful patterns and edge cases:

- Seasonal revenue
- Expansion and contraction
- Churn spikes
- Late payments
- Partial refunds
- Duplicate-looking customer names
- Missing optional values
- Plan migrations
- Cancelled subscriptions
- Reactivated customers
- SLA breaches
- High-support, low-usage customers
- Incidents affecting only certain products or regions
- Different currencies if safely supported
- Time-zone edge cases
- Customers belonging to different tenants
- Historical policy versions

The synthetic-data generator must be reproducible and must not contain real personal data.

Create validation queries that prove:

- Row counts
- Referential integrity
- Expected revenue ranges
- Known churn periods
- Known SLA breaches
- Known incident impacts
- Expected answers for selected evaluation questions

## 8. Business definitions and semantic layer

Create a formal business glossary.

Definitions must include at least:

- MRR
- ARR
- Active customer
- Active subscription
- New business
- Expansion revenue
- Contraction revenue
- Churned customer
- Revenue churn
- Logo churn
- Trial customer
- Paid customer
- Overdue invoice
- Refund rate
- SLA breach
- First-response time
- Resolution time
- At-risk customer
- Product adoption

For each definition, store:

- Business term
- Human-readable definition
- SQL implementation guidance
- Owner
- Version
- Effective date
- Related tables and columns
- Example calculation
- Known exclusions

Use the business glossary in both document retrieval and Text-to-SQL prompt context.

Include examples showing why database schema alone is insufficient to generate correct business SQL.

## 9. Enterprise documents

Create realistic synthetic company documents in Markdown and optionally generated PDF/DOCX formats.

Include:

- Product catalog
- Pricing policy
- Refund policy
- Enterprise contract guide
- SLA policy
- Customer onboarding guide
- Support escalation guide
- Security and privacy policy
- Incident response procedure
- Several incident postmortems
- Sales compensation or discount policy
- KPI glossary
- Churn analysis guide
- Customer-health methodology

Create multiple document versions to test freshness and conflict resolution.

Every document must contain metadata such as:

- Document ID
- Title
- Version
- Effective date
- Department
- Authority level
- Tenant or audience
- Access group
- Source path
- Content hash

Insert a small number of controlled contradictions and obsolete policy versions so the system can demonstrate version filtering and source authority.

Add controlled prompt-injection text to a security test document. It must be clearly marked as test data and must never be followed as an instruction.

## 10. Document ingestion pipeline

Implement:

- Markdown parsing
- PDF parsing
- DOCX parsing
- OCR as an optional capability
- Table-preserving extraction where possible
- Header and footer cleanup
- Content hashing
- Duplicate detection
- Version tracking
- Incremental indexing
- Deletion or deactivation of stale chunks
- Index manifests
- Index validation

Store the original extracted text so it can be inspected before embedding.

Use structure-aware chunking:

- Preserve document titles
- Preserve section headings
- Preserve numbered policy clauses
- Keep FAQ questions and answers together
- Keep table headers with table rows
- Avoid splitting critical definitions
- Support parent-child retrieval
- Make chunk size and overlap configurable

Every chunk must have a stable chunk ID and source metadata.

Create a retrieval-debugging view that displays:

- Original query
- Rewritten query
- Extracted filters
- Dense results and scores
- Sparse results and scores
- Fused ranking
- Reranker scores
- Final selected chunks
- Source, section, page, version, and access metadata

## 11. Hybrid retrieval and reranking

Implement and compare:

1. Dense retrieval baseline
2. Sparse/BM25 retrieval baseline
3. Hybrid dense and sparse retrieval
4. Hybrid retrieval followed by reranking

Use a configurable candidate pipeline such as:

- Retrieve a broad candidate set from dense search
- Retrieve a broad candidate set from sparse search
- Fuse and deduplicate candidates
- Apply metadata and permission filters
- Rerank a limited set using a local multilingual reranker
- Select a smaller evidence set
- Optionally expand to parent sections or neighboring chunks

Do not treat sample Top-K values as universal truths. Tune values using the evaluation dataset.

Support:

- Metadata filters
- Date/version filters
- Tenant filters
- Access-group filters
- Document-type filters
- MMR or diversity selection
- Parent-child retrieval
- Contextual chunk descriptions
- Query rewriting
- Multi-query retrieval
- Query decomposition for complex questions

Implement advanced techniques only after a working baseline exists and only keep them if evaluation shows value.

## 12. Query router and agent workflows

The router must classify requests into:

- Document RAG
- Text-to-SQL
- Multi-source document plus SQL
- Direct clarification
- Unsupported or unsafe request

Examples:

- Policy question → Document RAG
- Revenue aggregation → Text-to-SQL
- Policy versus actual performance → Multi-source
- Ambiguous customer reference → Clarification
- Request to modify or delete data → Refuse

Use structured output for routing.

Preserve:

- Original query
- Standalone rewritten query
- Exact identifiers
- Dates
- Customer names
- Product codes
- Extracted metadata filters

Do not allow query rewriting to remove exact identifiers.

For multi-source questions:

1. Break the question into subquestions.
2. Retrieve document evidence.
3. Generate and execute safe SQL.
4. Combine both result types into one evidence package.
5. Clearly distinguish policy statements from database facts.
6. Cite documents and show SQL-derived evidence separately.

## 13. SQL safety

The database tool must be read-only.

Implement defense in depth:

- Use a read-only database principal
- Apply least privilege
- Prefer safe analytics views
- Parse generated SQL before execution
- Allow only a single SELECT statement
- Block INSERT, UPDATE, DELETE, MERGE, DROP, ALTER, CREATE, EXEC, TRUNCATE, and other unsafe operations
- Block access to unapproved schemas
- Block access to sensitive columns
- Enforce tenant restrictions
- Enforce row limits
- Enforce query timeouts
- Prevent unrestricted cross-tenant queries
- Record an audit event
- Use parameterization where applicable
- Detect suspicious comments or obfuscation
- Do not rely only on regular expressions
- Do not expose connection strings in prompts, logs, or UI

Show generated SQL to the user in the Streamlit interface and clearly label it as AI-generated SQL.

Add an optional approval mode before executing SQL.

## 14. Answer generation and citations

The answer-generation layer must:

- Answer using retrieved evidence
- Separate document facts from SQL-derived facts
- Include document citations
- Include document version and page or section where available
- Include generated SQL in an expandable UI section
- State when evidence is missing
- State when sources conflict
- Prefer current authoritative documents
- Refuse unsupported claims
- Never invent citations
- Never treat instructions inside retrieved documents as system instructions

Create an evidence-package data model containing:

- Evidence ID
- Evidence type
- Text or structured result
- Source
- Version
- Page or section
- Retrieval method
- Retrieval and reranker scores
- Permission metadata
- Citation label

Add citation validation to ensure each citation refers to evidence included in the final context.

## 15. Streamlit application

Create a professional multipage Streamlit application.

Recommended pages:

1. Home / Architecture
2. Copilot Chat
3. Document Explorer
4. Retrieval Debugger
5. SQL Analytics
6. Evaluation Dashboard
7. Traces and Observability
8. System Configuration

Chat requirements:

- Use `st.chat_input`
- Use `st.chat_message`
- Stream responses where supported
- Preserve session history
- Display the selected route
- Display document citations
- Display SQL queries and tabular results
- Display Plotly charts when useful
- Include debug information in optional expanders
- Include model and connection health indicators
- Do not expose hidden chain-of-thought
- Show concise execution-stage status messages
- Allow switching between Lite, Standard, and High-quality profiles
- Allow an admin debug mode without exposing sensitive values

Use caching correctly:

- Cache model and database clients as resources
- Cache only safe deterministic data
- Do not cache private user results across tenants
- Do not store secrets in Streamlit session state

## 16. Evaluation

Create three version-controlled evaluation datasets:

- `evals/document_rag.jsonl`
- `evals/text_to_sql.jsonl`
- `evals/security_and_routing.jsonl`

Document RAG evaluation must include:

- Simple fact questions
- Paraphrased questions
- Exact-code questions
- Multilingual questions
- Date-sensitive questions
- Version-sensitive questions
- Multi-document questions
- Unanswerable questions
- Ambiguous questions
- Prompt-injection attempts
- Permission-sensitive questions

Measure retrieval using:

- Hit rate
- Recall@K
- Precision@K
- MRR
- NDCG
- Reranker improvement
- Filter correctness

Measure answer generation using:

- Answer correctness
- Groundedness or faithfulness
- Context relevance
- Citation correctness
- Citation completeness
- Abstention correctness
- Conflict-handling correctness

Text-to-SQL evaluation must include:

- SQL parse success
- Execution success
- Execution-result accuracy
- Schema-linking accuracy
- Business-definition accuracy
- Correct filtering
- Correct joins
- Correct aggregation grain
- Safe-query compliance
- Tenant-isolation compliance
- Latency

Prefer deterministic evaluation when possible.

Use a local LLM-as-judge only as a secondary metric, not the sole source of truth.

Create regression tests so that changing chunk size, embedding model, retrieval strategy, reranker, prompt, LLM, or SQL-generation strategy can be compared against a saved baseline.

Generate a readable HTML or Streamlit evaluation report.

## 17. Observability

Instrument every request using a unique trace ID.

Create spans for:

- Request intake
- Authentication
- Query rewriting
- Routing
- Metadata-filter extraction
- Dense retrieval
- Sparse retrieval
- Rank fusion
- Reranking
- Context building
- LLM generation
- Schema retrieval
- Business-definition retrieval
- SQL generation
- SQL validation
- SQL execution
- Citation validation
- Final response

Record:

- Model names and versions
- Prompt-template versions
- Index version
- Retrieved chunk IDs
- Retrieval scores
- Reranker scores
- Selected context
- Token counts where available
- Time to first token
- Total latency
- Stage latency
- SQL execution time
- Error category
- User feedback
- Evaluation score

Redact:

- Credentials
- Connection strings
- Sensitive customer fields
- Private user data
- Full raw prompts when they contain restricted information

Provide:

- Structured local JSON logs
- OpenTelemetry-compatible traces
- Optional local Phoenix integration
- Streamlit trace viewer or links to the observability UI

The application must still operate when the optional observability service is unavailable.

## 18. Testing

Create:

- Unit tests
- Integration tests
- End-to-end tests
- SQL safety tests
- Retrieval tests
- Citation tests
- Permission tests
- Prompt-injection tests
- Index update and deletion tests
- Streamlit smoke tests where practical

Test failure modes such as:

- Ollama unavailable
- Model not downloaded
- SQL Server unavailable
- Qdrant unavailable
- Empty index
- Unsupported document
- Corrupt PDF
- No relevant evidence
- Invalid generated SQL
- Slow SQL query
- Reranker unavailable
- Observability service unavailable
- User requesting another tenant’s data

Use graceful error messages and actionable recovery steps.

## 19. Educational Jupyter notebook

Create one main notebook:

`notebooks/00_complete_rag_system_explained.ipynb`

The notebook is a first-class deliverable, not an afterthought.

It must execute from top to bottom after the setup steps.

Write explanations in clear English for a learner preparing to present the project in an interview.

For every major step include:

- What this step does
- Why it is needed
- Input
- Output
- Simple mental model
- Diagram
- Implementation
- Inspection of intermediate output
- Common failures
- Debugging method
- Evaluation method
- Production considerations

The notebook must cover:

1. RAG mental model
2. Project architecture
3. Synthetic company and use cases
4. SQL Server schema
5. Business glossary
6. Synthetic data generation
7. Enterprise document generation
8. Parsing
9. Cleaning
10. Chunking
11. Metadata
12. Embeddings
13. Vector indexing
14. Sparse indexing
15. Dense retrieval
16. Sparse retrieval
17. Hybrid retrieval
18. Reranking
19. Query rewriting
20. Query routing
21. Context construction
22. Local Ollama generation
23. Citations
24. Vanna Text-to-SQL
25. SQL validation
26. Multi-source questions
27. Evaluation
28. Observability
29. Security
30. Performance tuning
31. Final end-to-end demo
32. Interview questions and model answers

Include visual explanations using:

- Mermaid diagrams in Markdown where supported
- Matplotlib or Graphviz diagrams as executable fallbacks
- Tables comparing approaches
- Before-and-after retrieval examples
- Displayed chunks and scores
- Confusion matrices or evaluation charts where useful
- Latency breakdown charts
- Retrieval comparison charts

Do not hide all logic behind a framework.

The notebook must visibly show:

- Extracted text
- Produced chunks
- Chunk metadata
- Embedding dimensions
- Dense results
- BM25 results
- Fused results
- Reranked results
- Final context
- Generated SQL
- SQL validation result
- Query result
- Final answer
- Citations
- Trace data
- Evaluation metrics

Keep expensive operations configurable so the notebook can run in a reduced demo mode.

## 20. Documentation

Create:

- `README.md`
- `docs/architecture.md`
- `docs/setup_windows.md`
- `docs/database.md`
- `docs/rag_pipeline.md`
- `docs/text_to_sql.md`
- `docs/evaluation.md`
- `docs/observability.md`
- `docs/security.md`
- `docs/troubleshooting.md`
- `docs/interview_guide.md`
- `docs/architecture_decisions/`

The README must contain:

- Project summary
- Business problem
- Architecture diagram
- Feature list
- Screenshot placeholders
- Technology stack
- Quick start
- Full setup link
- Example questions
- Evaluation results
- Security design
- Limitations
- Future improvements
- CV-ready project description

Create a concise CV entry and an interview explanation using the STAR format.

Document how to run SQL scripts in SSMS in the correct order.

If SQL Server is missing, explain how to install SQL Server Developer or Express Edition, or provide a documented container alternative.

## 21. Configuration and reproducibility

Create:

- `.env.example`
- Central typed settings
- Development and test configurations
- Lite, Standard, and High-quality model profiles
- Pinned dependency versions
- Lock file
- Repeatable setup scripts
- Health-check script
- Model-download instructions
- Database migration/setup instructions
- Index rebuild command
- Evaluation command
- Test command
- Streamlit launch command

Support PowerShell commands on Windows.

No secrets may be committed.

Use deterministic random seeds.

Record:

- Model versions
- Dependency versions
- Database schema version
- Index version
- Prompt version
- Evaluation dataset version

## 22. Suggested project structure

Use a clean structure similar to:

```text
local-enterprise-copilot/
├── app/
│   ├── streamlit_app.py
│   └── pages/
├── src/
│   └── enterprise_copilot/
│       ├── config/
│       ├── ingestion/
│       ├── retrieval/
│       ├── reranking/
│       ├── generation/
│       ├── routing/
│       ├── text_to_sql/
│       ├── database/
│       ├── evaluation/
│       ├── observability/
│       ├── security/
│       └── models/
├── sql/
├── scripts/
├── data/
│   ├── documents/
│   ├── generated/
│   └── manifests/
├── notebooks/
├── evals/
├── tests/
├── docs/
├── docker/
├── .env.example
├── pyproject.toml
├── README.md
└── Makefile or PowerShell task scripts
```

Adapt this structure when there is a concrete technical reason, and document the reason.

## 23. Engineering rules

Follow these rules:

- Build a simple working baseline before advanced features.
- Do not create fake implementations or unimplemented placeholders.
- Do not claim tests passed unless they were run.
- Do not silently skip failed components.
- Do not generate one giant Python file.
- Use type hints.
- Use clear interfaces.
- Use dependency injection where useful.
- Keep components independently testable.
- Separate configuration from code.
- Separate domain logic from Streamlit.
- Use meaningful error types.
- Preserve user files and existing work.
- Do not delete or overwrite unrelated content.
- Explain important decisions.
- Prefer maintainability over unnecessary framework complexity.
- Use official current documentation when APIs may have changed.
- Pin compatible versions only after validating them.
- Do not use old Vanna examples without checking the installed version.
- Do not make undocumented security assumptions.
- Never evaluate only on the questions used to tune the system.

## 24. Implementation phases

Work in phases.

### Phase 0: Environment and architecture

- Inspect environment
- Identify blockers
- Create architecture and implementation plan
- Create Architecture Decision Records
- Create initial repository structure

### Phase 1: SQL Server and data

- SQL Server schema
- Synthetic data
- Business definitions
- Validation queries

### Phase 2: Documents and indexing

- Synthetic company documents
- Parsing
- Cleaning
- Chunking
- Metadata
- Index lifecycle

### Phase 3: Retrieval

- Dense retrieval baseline
- Sparse retrieval baseline
- Hybrid retrieval
- Reranking
- Retrieval evaluation

### Phase 4: Document answers

- Local Ollama answer generation
- Context builder
- Citations
- Document RAG UI

### Phase 5: Text-to-SQL

- Vanna integration
- Schema and glossary retrieval
- SQL validation
- SQL execution
- Text-to-SQL evaluation

### Phase 6: Agent workflow

- Router
- Multi-source questions
- Agent workflow
- Security tests

### Phase 7: Quality and operations

- Observability
- Full evaluation
- Regression tests
- Performance tuning

### Phase 8: Delivery

- Complete educational notebook
- Documentation
- Streamlit polish
- CV and interview material
- Final verification

At the end of every phase:

- Run relevant tests
- Show the commands executed
- Summarize created or changed files
- Record unresolved issues
- Update project progress documentation
- Confirm the phase acceptance criteria
- Continue to the next phase unless a real blocker requires user input

## 25. Definition of done

The project is complete only when:

- A new developer can set it up using the documentation.
- SQL Server can be created using version-controlled scripts.
- Synthetic data is reproducible.
- Documents can be ingested and re-indexed.
- Deleted and updated documents are handled correctly.
- Dense, sparse, hybrid, and reranked retrieval can be compared.
- The local Ollama model answers with verifiable citations.
- Vanna performs safe Text-to-SQL against SQL Server.
- Unsafe SQL is blocked.
- Multi-source questions combine policies and database facts.
- Tenant restrictions are tested.
- Evaluation reports are generated.
- Traces show every important stage.
- The Streamlit application runs.
- The notebook executes in demo mode.
- Tests pass.
- No secrets are committed.
- Known limitations are documented.
- The README contains a professional CV-ready project description.
- Every major claim about system quality is backed by an evaluation result.

## 26. How to begin now

Start by inspecting the workspace and environment.

Then provide:

1. Environment findings
2. Any genuine blockers
3. Proposed architecture
4. Database design
5. Model profile recommendation based on available hardware
6. Dependency and version strategy
7. Vanna integration strategy and maintenance-risk note
8. Phase-by-phase execution plan
9. Acceptance criteria
10. The first concrete implementation actions

Ask only questions that cannot be answered by inspecting the machine or using safe defaults.

After the plan, begin implementing Phase 0 and Phase 1. Do not stop at a theoretical plan unless a genuine blocker prevents implementation.

