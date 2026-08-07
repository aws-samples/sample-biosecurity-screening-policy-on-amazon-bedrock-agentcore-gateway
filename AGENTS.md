# AGENTS.md

## Project Overview

AWS CDK project deploying scientific research tools as MCP-compatible services behind Amazon Bedrock AgentCore Gateway, with gateway-wide interception and targeted biosafety screening for ordering requests.

**Architecture:**

```text
MCP Client → AgentCore Gateway → Biosafety Interceptor ──┬── MMseqs2 Screener
  (SigV4)      (IAM inbound auth)                         ├── Embedding Screener → ESMC-600M Endpoint
                         │                                └── Foldseek Screener → Foldseek+ProstT5 Endpoint (CPU)
              ┌──────────┼─────────────────────┐
              ▼          ▼                     ▼
     search-pmc Lambda  uniprot-search   ordering-tool Lambda
              │          Lambda
              ▼             ▼
      NCBI E-utilities   UniProt REST API
```

## Directory Map

```text
research-gateway/
├── app.py                        # Thin CDK entrypoint → synth_app()
├── cdk.json                      # CDK config with context flags
├── pyproject.toml                # Project metadata, dependencies, pytest config
├── scripts/
│   ├── invoke_gateway.py        # SigV4-signed MCP client — smoke-tests the deployed gateway
│   ├── prepare_foldseek_structures.py  # Download threat PDBs into the Foldseek build context
│   ├── download_pdb.py          # Archive threat PDB structures to S3
│   └── embed_fasta.py           # Regenerate the embedding reference index via the ESMC endpoint
├── references/                  # Background reading (biosafety research, design notes)
├── schemas/tools/                # MCP tool schemas, versioned with the code
│   ├── search_pmc.json
│   ├── search_uniprot.json
│   └── ordering.json
├── deploy/                       # Deployment-only assets (Docker contexts, Lambda layers)
│   ├── containers/
│   │   ├── mmseqs2/
│   │   │   ├── Dockerfile           # Lambda container image with MMseqs2 binary + pre-built DB
│   │   │   ├── handler.py           # Runs MMseqs2 search, returns hits + min_evalue
│   │   │   └── SafeProtein_Bench.fasta  # Threat sequence database (429 deduplicated proteins)
│   │   ├── esmc/                    # ESMC-600M SageMaker BYOC container
│   │   └── foldseek/                # Foldseek+ProstT5 SageMaker BYOC container (+ structures/, gitignored)
│   └── layers/
│       ├── search/requirements.txt      # Runtime deps: httpx, defusedxml, boto3
│       └── embedding/requirements.txt   # numpy (Lambda layer)
└── src/research_gateway/         # Importable application package
    ├── infrastructure/
    │   ├── application.py       # build_app() / synth_app() → BiosafetyStack, ResearchGatewayStack
    │   ├── biosafety_stack.py   # ESMC endpoint, MMseqs2 + embedding + interceptor Lambdas
    │   ├── gateway_stack.py     # AgentCore Gateway, PolicyEngine, search-pmc + uniprot-search + ordering-tool Lambdas
    │   └── paths.py             # Centralized filesystem locations (SOURCE_ROOT, DEPLOY_ROOT, ...)
    ├── tools/
    │   ├── pmc/
    │   │   ├── handler.py       # Lambda handler — validates input, returns MCP envelope
    │   │   └── search.py        # Core logic — NCBI search, XML parse, citation rerank
    │   ├── uniprot/
    │   │   ├── handler.py       # Lambda handler — validates input, returns MCP envelope
    │   │   └── search.py        # Core logic — UniProt search, concurrent detail fetch, flatten
    │   └── ordering/
    │       └── handler.py       # Mock ordering tool — returns random order ID
    ├── biosafety/
    │   ├── interceptor.py       # REQUEST interceptor — runs dual screening, injects risk scores
    │   └── sequence_finder.py   # Regex extractor for amino acid sequences (≥20 residues)
    └── screening/embedding/
        ├── handler.py           # Embeds sequence via ESMC-600M, cosine search vs reference index
        └── data/safeprotein_bench_index.npz  # Pre-normalised reference embeddings (429 × 1152 float32)
```

## Testing

`make test` is hermetic: it runs the unit and infrastructure suites while the deployed gateway tests skip unless explicitly enabled.

```bash
make test-unit
make test-infra
make test-gateway-integration  # MCP initialization, tool list, permitted PMC call, Cedar deny check
make test-gateway-full         # baseline checks plus ordering-tool biosafety allow/deny checks
```

`tests/integration/test_gateway_e2e.py` loads `GatewayClient` from `scripts/invoke_gateway.py`, retaining the smoke test's SigV4 signing and MCP `initialize`/`notifications/initialized` session flow. The integration target needs a deployed `ResearchGatewayStack` plus ambient credentials with `bedrock-agentcore:InvokeGateway` on its `GatewayArn`. Its default `GatewayUrl` lookup also needs `cloudformation:DescribeStacks`; pass `--gateway-url` to avoid that lookup, or use `--gateway-stack` and `--gateway-region` to select another deployment:

```bash
uv run pytest --run-gateway-integration tests/integration \
  --gateway-url "$GATEWAY_URL" --gateway-region us-west-2
```

`make test-gateway-full` also requires the deployed `BiosafetyStack`. It invokes MMseqs2, ESMC, and Foldseek through the mock `ordering-tool`, so it can incur ESMC's GPU endpoint cost (Foldseek's endpoint is CPU-only) and higher latency but never places a real order. It is deliberately separate from the lower-latency integration target.

## Key Entry Points

| Task | File | Symbol |
|------|------|--------|
| Deploy infrastructure | `app.py` → `src/research_gateway/infrastructure/application.py` | `synth_app()`, `build_app()` |
| CDK stack — Biosafety | `src/research_gateway/infrastructure/biosafety_stack.py` | `BiosafetyStack.__init__` |
| CDK stack — Gateway | `src/research_gateway/infrastructure/gateway_stack.py` | `ResearchGatewayStack.__init__` |
| Handle Lambda invocation | `src/research_gateway/tools/pmc/handler.py` | `handler()` |
| Execute PMC search | `src/research_gateway/tools/pmc/search.py` | `search_pmc_structured()` |
| Handle UniProt invocation | `src/research_gateway/tools/uniprot/handler.py` | `handler()` |
| Execute UniProt search | `src/research_gateway/tools/uniprot/search.py` | `search_uniprot_structured()` |
| Intercept gateway requests | `src/research_gateway/biosafety/interceptor.py` | `lambda_handler()` |
| Screen sequences (alignment) | `deploy/containers/mmseqs2/handler.py` | `lambda_handler()` |
| Screen sequences (embedding) | `src/research_gateway/screening/embedding/handler.py` | `lambda_handler()` |
| Call the gateway (SigV4) | `scripts/invoke_gateway.py` | `GatewayClient`, `main()` |

## Patterns and Conventions

**MCP Response Envelope:** All Lambda responses use `{content: [{type, text}], structuredContent: {...}, isError: bool}`. `_success()` and `_error()` helpers in each tool's `handler.py` enforce this shape.

**AgentCore Tool Naming:** Gateway prefixes tool names with target name + `___` delimiter (e.g., `pmc-search___search_pmc`). Lambda handlers strip this prefix before dispatching.

**Inbound Auth (IAM SigV4):** The gateway is created with `GatewayAuthorizer.using_aws_iam()`. Callers SigV4-sign requests and need `bedrock-agentcore:InvokeGateway` on the `GatewayArn` output; there are no bearer tokens or user pools. `authorizer_configuration` must always be passed explicitly — omitting it does **not** default to IAM, it makes the construct provision its own Cognito user pool, client, domain, and resource server inside the stack. MCP clients that cannot sign (e.g. MCP Inspector) go through `mcp-proxy-for-aws` with `--service bedrock-agentcore`.

The former `CognitoStack` (`cognito_stack.py`) and the `get_user_token.py` / `get_m2m_token.py` scripts were deleted in this migration. A `CognitoStack` found in CloudFormation is an orphan from the old deployment, not a stack this repo still defines — don't try to reconcile it with the code.

**One Lambda Per Tool:** Each gateway target gets its own Lambda and its own single-tool schema file. Handlers guard with `if tool_name and tool_name != "<tool>": return _error(...)` rather than routing an if/elif dispatch table. The `_extract_tool_name` / `_success` / `_error` helpers are duplicated per handler (`tools/pmc/handler.py`, `tools/uniprot/handler.py`) even though both now live in the same importable package — consolidating them is a deliberately deferred behavioral refactor, not a migration leftover.

**Query Syntax Passthrough:** Both search tools accept their upstream's native query syntax. `search_pmc` passes PMC syntax straight through; `search_uniprot` detects a UniProt field token (`_FIELD_TOKEN_PATTERN` in `tools/uniprot/search.py`) and skips field expansion when it finds one. This is how callers reach filters that are not exposed as tool parameters — notably review status (`reviewed:true`), since `search_uniprot` exposes only `query` and `species`. The reference implementation this was adapted from defaulted `organism` to human, which silently zeroed out non-human queries; that default was deliberately dropped in favor of an optional, unset-by-default `species` parameter.

**Optional Species Filter:** `search_uniprot`'s `species` parameter is ANDed onto the built query via `_resolve_species_filter()`, which passes the value to `organism_name` verbatim — there is no common-name mapping. Callers must supply the full scientific name UniProt recognizes (e.g. `"Homo sapiens"`, `"Severe acute respiratory syndrome coronavirus 2"`); the tool schema description says so explicitly, since `organism_name` does loose text matching rather than exact matching and will happily accept — and mismatch on — a vague or common-name value like `"human"` or `"coronavirus"` instead of rejecting it. Combining `species` with a query that already contains its own `organism_name` field filter ANDs both together and can produce a contradictory query — that combination is documented as unsupported, not guarded against in code.

**Biosafety Screening:** Every `tools/call` passes through the interceptor Lambda, but the Lambda only extracts sequences and invokes screeners for the `ordering-tool` target. Calls to `search-pmc` and `uniprot-search` pass through unmodified. For an ordering request, it extracts amino acid sequences using `[ACDEFGHIKLMNPQRSTVWY]{20,}` regex, then runs all three screens in parallel (via `ThreadPoolExecutor`) across all detected sequences simultaneously:

1. **MMseqs2** — sequence alignment against SafeProtein_Bench. Computes `mmseqs_risk_score = -log10(min_evalue)`. Blocked if `> mmseqs_risk_threshold` (default: 5, equivalent to E-value < 1e-5).
2. **Embedding** — encodes the sequence via ESMC-600M, mean-pools per-residue embeddings, computes cosine similarity against the pre-normalised SafeProtein_Bench reference index. Computes `embedding_risk_score = int(max_similarity × 100)`. Blocked if `> embedding_risk_threshold` (default: 95, equivalent to cosine similarity > 0.95).
3. **Foldseek** — translates the sequence to the 3Di structural alphabet with ProstT5 (inside a CPU-only SageMaker endpoint, `ml.c6i.2xlarge`), then searches a database of 429 threat protein structures. Computes `foldseek_risk_score = -log10(min_evalue)`. Blocked if `> foldseek_risk_threshold` (default: 5). Skipped silently if `FOLDSEEK_ENDPOINT_NAME` is unset.

The interceptor is registered `for_request`, so it screens tool **arguments only** — never responses. Since it explicitly checks the `<target>___<tool>`-prefixed name, new targets are **not** screened until the target guard is deliberately changed. The consequence for `search_uniprot`: the amino acid sequences it *returns* are unscreened, and a sequence in a `search_pmc` or `search_uniprot` request is also unscreened; only a later `ordering_tool` call triggers screening.

Any screen can independently block a request (fail-closed). All three risk scores are injected as flat scalars (`_biosafety_mmseqs_risk_score`, `_biosafety_embedding_risk_score`, `_biosafety_foldseek_risk_score`) plus `_biosafety_sequences_found`, `_biosafety_screened_at`, and `_biosafety_embedding_max_similarity` for audit.

**Cedar Context Constraint:** AgentCore serializes nested dicts in `context.input` as JSON strings. All values injected by the interceptor for Cedar evaluation must be flat scalars.

**Cross-Stack Wiring:** `BiosafetyStack` exposes `interceptor_function`, `mmseqs_risk_threshold`, `embedding_risk_threshold`, and `foldseek_risk_threshold`. `ResearchGatewayStack` accepts all four — it attaches the interceptor to the gateway and registers the Cedar forbid policies with the correct thresholds.

**Cedar Policy Construction:** The biosafety forbid policies are generated as f-strings in `gateway_stack.py` using the gateway ARN (available only there) and the threshold values passed from `BiosafetyStack`. Each method gets its own `add_policy` call — the API accepts one Cedar statement per policy.

**License Filtering:** `_build_search_query()` in `tools/pmc/search.py` appends CC license filters to every PMC query. Controlled by `COMMERCIAL_USE_ONLY` env var (defaults `True`).

**Layer Bundling:** `PythonLayerVersion` uses Docker to build Linux-compatible wheels. Docker must be running for `cdk deploy`.

**ESMC-600M Endpoint:** Deployed as a SageMaker real-time endpoint from a self-built BYOC container (`deploy/containers/esmc/`) serving the open-source [`biohub/ESMC-600M`](https://huggingface.co/biohub/ESMC-600M) weights. One call per sequence: the Lambda posts `{"sequence": "..."}` and receives `{"embedding": [...1152 floats...]}` — tokenization, BOS/EOS stripping and mean-pooling all happen inside the container.

ESMC is **not** in upstream `transformers` (the HF config declares `model_type: "esmc"` but ships no modeling code and no `auto_map`, so `trust_remote_code` cannot help). The loader lives in Biohub's `transformers` fork, installed from a commit-pinned codeload tarball in the Dockerfile — `python:3.12-slim` has no `git`, and the pin matters because CDK's `DockerImageAsset` hashes the build context, not resolved dependencies. `AutoModel` resolves to `ESMCModel` (the bare encoder) via `model_type`; `last_hidden_state` is the analogue of the old Forge `embeddings`.

Container serving notes, each load-bearing: **no `--preload`** (it would initialise CUDA in the gunicorn arbiter, and a CUDA context does not survive fork) — `--timeout 0` disables the watchdog instead, since the model loads during the worker's import. `--threads 4` selects the `gthread` worker so a concurrent `GET /ping` is not starved behind queued inference; `/ping` has a hard 2s timeout and a non-200 gets the instance replaced. Weights are baked to `/opt/esmc-600m` at build time, so the endpoint runs under `EnableNetworkIsolation`.

**Biosafety caveat:** the Marketplace envelope carried a `potential_sequence_of_concern` flag and could refuse to return embeddings. The self-built container has no such upstream gate — it will embed any sequence it is given, subject only to `MAX_SEQUENCE_LENGTH`. The three screening layers are unchanged; what is gone is the vendor's own refusal on the embedding call.

## Configuration

| Variable / Context Key | Source | Purpose |
|------------------------|--------|---------|
| `NCBI_API_KEY` | Lambda env var | Raises NCBI rate limit from 3 → 10 req/sec |
| `COMMERCIAL_USE_ONLY` | Lambda env var | CC license filter on PMC queries (default: `True`) |
| `mmseqs_risk_threshold` | CDK context | MMseqs2 risk score above which requests are blocked (default: `5`) |
| `embedding_risk_threshold` | CDK context | Embedding risk score above which requests are blocked (default: `95`) |
| `foldseek_risk_threshold` | CDK context | Foldseek structural risk score above which requests are blocked (default: `5`) |
| `esmc_instance_type` | CDK context | SageMaker instance type for ESMC-600M (default: `ml.g5.xlarge`) |
| `esmc_instance_count` | CDK context | Number of ESMC-600M endpoint instances (default: `1`) |
| `foldseek_instance_type` | CDK context | SageMaker instance type for Foldseek+ProstT5 (default: `ml.c6i.2xlarge`) |

## Custom Instructions
<!-- This section is for human and agent-maintained operational knowledge.
     Add repo-specific conventions, gotchas, and workflow rules here.
     This section is preserved exactly as-is when re-running codebase-summary. -->
