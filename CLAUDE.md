# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

A CDK app that deploys a PubMed Central (PMC) search Lambda behind an Amazon Bedrock AgentCore Gateway, exposed as an MCP tool. Inbound auth is IAM (SigV4); a Cedar PolicyEngine is attached in ENFORCE mode.

`MCP Client → AgentCore Gateway (IAM SigV4) → Lambda (search-pmc → NCBI E-utilities, uniprot-search → UniProt REST, ordering-tool)`

## Commands

All commands run from the repository root. A `Makefile` wraps the common ones — run `make help` for the full list.

```bash
make sync           # uv sync
source .venv/bin/activate

make synth          # cdk synth
make deploy-all      # cdk deploy --all (BiosafetyStack + ResearchGatewayStack)
make deploy          # cdk deploy ResearchGatewayStack
make diff            # cdk diff
```

The underlying `uv run cdk ...` / `uv sync` commands still work directly if you'd rather not use `make`.

Docker must be running — `PythonLayerVersion` bundles the Lambda layer in a Linux container.

Tests live under `tests/`: `tests/unit` contains Lambda-handler tests, `tests/infrastructure` contains CDK stack assertions, and `tests/integration` contains opt-in checks against a deployed AgentCore Gateway.

```bash
make test           # hermetic unit + infrastructure suite; deployed checks skip by default
make test-unit       # tests/unit only
make test-infra      # tests/infrastructure only
make test-gateway-integration  # deployed MCP list, PMC, and Cedar-policy checks
make test-gateway-full         # also invoke deployed ordering-tool screeners
```

The integration tests load `GatewayClient` from `scripts/invoke_gateway.py`, preserving the smoke test's SigV4 and MCP initialization flow. The standard target requires a deployed `ResearchGatewayStack`, `bedrock-agentcore:InvokeGateway` on its `GatewayArn`, and (unless `--gateway-url` is supplied) `cloudformation:DescribeStacks` to obtain `GatewayUrl`. Use `--gateway-stack`, `--gateway-region`, or `--gateway-url` with `uv run pytest --run-gateway-integration tests/integration` to target a non-default deployment.

`make test-gateway-full` includes the standard deployed checks and invokes MMseqs2, ESMC, and Foldseek through `ordering-tool`. It needs the deployed `BiosafetyStack`, invokes the ESMC GPU-backed endpoint (Foldseek's endpoint is CPU-only, on `ml.c6i.2xlarge`), and is intentionally separate because of latency and AWS cost; the order target remains a mock. `tests/infrastructure` synthesizes stacks with the `aws:cdk:bundling-stacks: []` context key, so it does not need Docker. Unit and infrastructure tests do not build or run the ML containers; the full gateway test reaches the deployed screeners but is not a container-runtime test.

Beyond automated tests, the repo is exercised through direct Lambda invocation and the MCP Inspector (see [`README.md`](README.md#implementation-and-component-testing)).

Calling the gateway needs no token — it uses IAM (SigV4) inbound auth, so ambient AWS credentials
are enough, provided they carry `bedrock-agentcore:InvokeGateway` on the `GatewayArn` stack output.

MCP clients can't sign SigV4 themselves. Front the gateway with `mcp-proxy-for-aws`, a local stdio
MCP server that signs and forwards:

```bash
uvx mcp-proxy-for-aws@latest "$GATEWAY_URL" --service bedrock-agentcore --region us-east-1
```

`--service bedrock-agentcore` is required; the proxy does not infer the signing service from the URL.

## Stack topology

The root `app.py` calls `synth_app()` in `src/research_gateway/infrastructure/application.py`, whose `build_app()` instantiates two stacks with a cross-stack reference:

- `BiosafetyStack` — owns the ESMC-600M and Foldseek+ProstT5 SageMaker endpoints, the MMseqs2 and embedding screening Lambdas, and the biosafety interceptor Lambda. Exposes `interceptor_function` plus the three risk thresholds.
- `ResearchGatewayStack` — receives `interceptor_function` and the thresholds via constructor kwargs. Creates the gateway with `GatewayAuthorizer.using_aws_iam()`, plus a `PolicyEngine` attached in `ENFORCE` mode.

Both stacks live in `src/research_gateway/infrastructure/`; `paths.py` centralizes the filesystem locations (`SOURCE_ROOT`, `DEPLOY_ROOT`/`CONTAINERS_ROOT`/`LAYERS_ROOT`, `SCHEMAS_ROOT`) each stack uses to locate Lambda source, containers, layers, and schemas.

`gateway.add_interceptor(...)` in `gateway_stack.py` attaches the biosafety interceptor gateway-wide — AgentCore Gateway interceptors cannot be scoped to a single target at the infrastructure level (a gateway has at most one `REQUEST` and one `RESPONSE` interceptor total, confirmed against both the CDK L1/L2 constructs and the `GatewayInterceptorConfiguration`/`CreateGatewayTarget` API reference — neither has a target- or tool-scoping field). `src/research_gateway/biosafety/interceptor.py` therefore does the scoping itself: it reads the `<target>___<tool>`-prefixed `params.name` off the incoming request and only runs screening when the target is `ordering-tool` (matching `gateway_target_name="ordering-tool"` in `gateway_stack.py`); calls to `pmc-search` and `uniprot-search` pass through unmodified.

`authorizer_configuration` must always be passed explicitly. Its documented default is **not** IAM — omitting it makes the construct provision its own Cognito user pool, client, domain, and resource server inside the stack.

Cedar policies are defined inline in `gateway_stack.py`: a permissive `AllowAll` (dev placeholder) and three `BiosafetyForbid*` rules that block requests whose injected risk scores exceed their thresholds. `forbid` overrides `permit`, so adding deny rules is the intended pattern for tightening access.

All four policies use a bare `principal` and condition only on `context.input.*`, which is why the auth change did not touch them. Note that this construct version only documents `AgentCore::OAuthUser` as a non-wildcard principal type and sources principal tags from an OAuth token — under SigV4 there is no token, so principal-scoped Cedar policies aren't expressible here.

## Gateway/Lambda invocation contract

When invoked through the gateway, AgentCore prefixes the tool name with the target name and a `___` delimiter (e.g. `pmc-search___search_pmc`). `src/research_gateway/tools/pmc/handler.py:_extract_tool_name` strips this prefix. If you rename the gateway target, the handler still works — but log lines and Cedar action IDs will reflect the new prefix.

All handler responses use the MCP envelope `{content: [...], structuredContent: {...}, isError: bool}` via the `_success`/`_error` helpers. Don't return raw dicts.

The gateway flattens the tool's `inputSchema` properties onto the Lambda event, so the handler reads `event["query"]` directly — there is no separate "arguments" wrapper.

## Tool schema

Each target has its own single-tool schema file, uploaded via `ToolSchema.from_local_asset`. Schema changes redeploy with `cdk deploy`. Keep each in sync with its handler's accepted parameters:

- `schemas/tools/search_pmc.json` → `search_pmc` (`query`, `rerank_by`)
- `schemas/tools/search_uniprot.json` → `search_uniprot` (`query`, `species`)
- `schemas/tools/ordering.json` → `ordering_tool` (`inputs`)

## Search behavior gotchas

- `_build_search_query` in `src/research_gateway/tools/pmc/search.py` always appends a CC-license filter when `COMMERCIAL_USE_ONLY` is truthy (env var, defaults to `True` — note: env vars are strings, so any non-empty value is truthy).
- `rerank_by="references"` builds an intra-result-set citation graph from PMID cross-references, then sorts descending. It does not query external citation APIs.
- `NCBI_API_KEY` is read from the Lambda environment if set (raises rate limit 3→10 req/s) but is **not** wired into the CDK stack — set it manually on the function or add it to the stack if you need it.
- `_build_search_query` in `src/research_gateway/tools/uniprot/search.py` passes a query through untouched when it contains a UniProt field token (`gene:`, `organism_name:`, `reviewed:true`, …) and otherwise expands it across name/gene/function/disease/keyword fields. There is no *default* organism filter — the aws-samples reference this was adapted from defaulted to human, which returns zero results for queries like "SARS-CoV-2 spike protein" — but there is an optional `species` parameter (see below) plus field passthrough for anything else, like review status.
- `species` (optional) is ANDed onto the built query via `_resolve_species_filter`, which passes the value to `organism_name` verbatim with **no** common-name mapping. Callers must pass a scientific name (`"Homo sapiens"`, not `"human"`) — `organism_name` does loose text matching, so a common or vague name may match anyway, but not necessarily what the caller intended (`"coronavirus"` matches several unrelated species). Setting `species` on a query that already contains its own `organism_name` field filter ANDs both together and can conflict — that's documented as unsupported usage, not validated against.
- `search_uniprot` makes `1 + N` HTTP calls per invocation (one search, one detail fetch per hit) over a single pooled `httpx.Client`, fanned out via `ThreadPoolExecutor`. `DEFAULT_LIMIT` caps N at 10. A detail fetch that fails is logged and dropped, so `returned` can be lower than the search hit count rather than the whole call failing.
- `search_uniprot`'s `total_found` counts entries matching the *expanded* query, not relevant hits — plain-text expansion includes clauses like `keyword:"protein"`, so it routinely reads in the tens of millions. It is only precise when the caller passed explicit field syntax. `_format_summary` deliberately omits it; don't reintroduce it into the text summary.
- UniProt `commentType` values are **space**-separated (`"SUBCELLULAR LOCATION"`, not `"SUBCELLULAR_LOCATION"`) — see the constants at the top of `src/research_gateway/tools/uniprot/search.py`. Getting this wrong fails silently: the comment never matches and the field returns empty. The aws-samples reference this was adapted from has that bug.
- `DETAIL_FIELDS` requests only `ft_domain` and `ft_region`, so `features` is empty for entries annotated solely with signal peptides or disulfide bonds (insulin, for one).
- `search_uniprot` **returns** amino acid sequences. The biosafety interceptor is `for_request` only, so those sequences are not screened on the way out — and since the interceptor only screens calls to `ordering-tool` (see below), they are not screened on the way in either. Don't assume anything reaching an agent through this tool has passed screening.

## External documentation

`AGENTS.md` contains a longer documentation map (architecture, directory layout, entry points, conventions, configuration). Consult it when you need depth beyond this file.

## Production safety

The repo deploys to whatever AWS account your credentials point at. Treat any deployed `ResearchGatewayStack` / `BiosafetyStack` as production unless you have direct evidence otherwise — the stack names contain no environment suffix. Before destructive actions (`cdk destroy`, deleting SageMaker endpoints, removing the policy engine), confirm with the user.

`cognito_stack.py` was deleted when the gateway moved to IAM auth, but a `CognitoStack` may still be **deployed** from before the switch — dropping it from `app.py` orphaned it rather than deleting it. Do not treat that orphan as drift to reconcile. Note that `cdk destroy CognitoStack` cannot work (the stack is not in the app any more); deleting it means `aws cloudformation delete-stack --stack-name CognitoStack`, which is destructive and leaves the `RemovalPolicy.RETAIN` user pool behind. Confirm with the user before touching it.
