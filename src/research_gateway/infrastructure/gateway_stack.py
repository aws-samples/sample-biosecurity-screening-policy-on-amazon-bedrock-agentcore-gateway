# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""CDK stack that deploys the PMC search Lambda and its dependency layer,
fronted by an Amazon Bedrock AgentCore Gateway with a Lambda target."""

from aws_cdk import (
    ArnFormat,
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
)
from aws_cdk import aws_bedrock_agentcore_alpha as agentcore
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as _lambda
from aws_cdk import aws_logs as logs
from aws_cdk.aws_lambda_python_alpha import PythonLayerVersion
from constructs import Construct

from research_gateway.infrastructure.paths import LAYERS_ROOT, SCHEMAS_ROOT, SOURCE_ROOT

LAYER_DIR = LAYERS_ROOT / "search"
TOOL_SCHEMA_PATH = SCHEMAS_ROOT / "search_pmc.json"
ORDERING_TOOL_SCHEMA_PATH = SCHEMAS_ROOT / "ordering.json"
UNIPROT_TOOL_SCHEMA_PATH = SCHEMAS_ROOT / "search_uniprot.json"

# Species names blocked from the search_uniprot tool's `species` parameter.
BLOCKED_UNIPROT_SPECIES = ["Clostridium botulinum", "Gloydius halys"]

# Retention for every CloudWatch log group this stack owns — the two Lambda log groups
# and the gateway application-log group. Kept uniform deliberately: the gateway logs
# include request and response bodies (screened sequences, PMC abstracts), so raising
# this raises both the storage bill and how long that data is retained.
LOG_RETENTION = logs.RetentionDays.ONE_WEEK


class ResearchGatewayStack(Stack):
    """Deploys the PMC search Lambda behind an AgentCore Gateway."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        interceptor_function: _lambda.Function,
        mmseqs_risk_threshold: int,
        embedding_risk_threshold: int,
        foldseek_risk_threshold: int,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        runtime = _lambda.Runtime.PYTHON_3_12

        # Build a layer from requirements.txt. PythonLayerVersion uses a
        # container to produce Linux-compatible wheels, so keep the entry
        # directory small and isolated (just requirements.txt).
        dependencies_layer = PythonLayerVersion(
            self,
            "SearchPmcDependenciesLayer",
            entry=str(LAYER_DIR),
            compatible_runtimes=[runtime],
            description="Shared dependencies for the PMC and UniProt search Lambdas (httpx, defusedxml, boto3).",
        )

        # Shared source asset for every Python Lambda in this stack — the whole
        # research_gateway package, addressed by package-qualified handler paths.
        # Favors reliability over minimum artifact size; per-function bundling can
        # be introduced later if artifact size becomes material.
        source_code = _lambda.Code.from_asset(str(SOURCE_ROOT))

        # Explicit log group with a retention policy (replaces the deprecated
        # logRetention property on Function, which provisioned a custom
        # resource Lambda to adjust retention after the fact).
        log_group = logs.LogGroup(
            self,
            "SearchPmcFunctionLogGroup",
            retention=LOG_RETENTION,
            removal_policy=RemovalPolicy.DESTROY,
        )

        search_pmc_function = _lambda.Function(
            self,
            "SearchPmcFunction",
            function_name="search-pmc",
            runtime=runtime,
            handler="research_gateway.tools.pmc.handler.handler",
            code=source_code,
            layers=[dependencies_layer],
            timeout=Duration.seconds(60),
            memory_size=512,
            log_group=log_group,
            description="Searches PubMed Central (PMC) with optional citation-based reranking.",
        )

        # PolicyEngine container for Cedar authorization policies.
        policy_engine = agentcore.PolicyEngine(
            self,
            "SearchPmcPolicyEngine",
            policy_engine_name="search_pmc_policy_engine",
            description="Policy engine for the PMC search gateway.",
        )

        # Gateway names have to be unique within the account. AgentCore appends its
        # own random 10-character suffix to the gateway *identifier*, but not to the
        # name, so derive one here from the construct path — deterministic, so
        # redeploys keep the same name instead of churning it.
        gateway_name = f"research-gateway-{self.node.addr[:8]}"

        # AgentCore Gateway with IAM (SigV4) inbound auth and PolicyEngine attached.
        # authorizer_configuration must be passed explicitly — omitting it does not
        # default to IAM, it makes the construct provision its own Cognito user pool,
        # client, domain, and resource server inside this stack.
        #
        # The construct scopes the gateway service role's trust policy and the
        # PolicyEngine grant to `:gateway/{gateway_name}*`, but the gateway identifier
        # is fixed at creation time. Renaming an existing gateway in place therefore
        # leaves those conditions pointing at a prefix the live ARN no longer matches,
        # which breaks the role assumption and Cedar evaluation. Rename the construct
        # ID alongside gateway_name so CloudFormation replaces the gateway instead —
        # note that this changes the GatewayUrl and GatewayArn outputs.
        gateway = agentcore.Gateway(
            self,
            "ResearchGateway",
            gateway_name=gateway_name,
            description="AgentCore Gateway fronting the research tool Lambdas.",
            authorizer_configuration=agentcore.GatewayAuthorizer.using_aws_iam(),
            policy_engine_configuration=agentcore.GatewayPolicyEngineConfig(
                policy_engine=policy_engine,
                mode=agentcore.PolicyEngineMode.ENFORCE,
            ),
        )

        # Grant the gateway execution role permissions to evaluate policies against
        # this policy engine. Required for policy enforcement to work.
        policy_engine.grant_evaluate_for_gateway(gateway.role, gateway)

        # Grant CheckAuthorizePermissions for deployment-time validation.
        # CloudFormation needs this to validate the PolicyEngine attachment.
        # The resource format is unusual: /policy-engines/{id}/target-resource/*
        gateway.role.add_to_policy(
            iam.PolicyStatement(
                actions=["bedrock-agentcore:CheckAuthorizePermissions"],
                resources=[
                    f"arn:aws:bedrock-agentcore:{Stack.of(self).region}:{Stack.of(self).account}:/policy-engines/{policy_engine.policy_engine_id}/target-resource/*",
                ],
            )
        )

        # Permissive Cedar policy — allows all principals to perform all actions on
        # this gateway. Intended as a starting point; tighten before production use.
        policy_engine.add_policy(
            "AllowAll",
            definition=f'permit(principal, action, resource == AgentCore::Gateway::"{gateway.gateway_arn}");',
            description="Allow all requests through (development only).",
            validation_mode=agentcore.PolicyValidationMode.IGNORE_ALL_FINDINGS,
        )

        # Per-tool permit for the UniProt search target. Redundant while AllowAll is
        # in place — it exists as the hook to keep once AllowAll is removed for
        # production. Cedar policies here cannot scope to a single tool: the only
        # non-wildcard principal type this construct version documents is
        # AgentCore::OAuthUser and its tags come from an OAuth token, but inbound auth
        # is SigV4, so there is no token to source them from.
        policy_engine.add_policy(
            "AllowUniProtSearch",
            definition=f'permit(principal, action, resource == AgentCore::Gateway::"{gateway.gateway_arn}");',
            description="Allow requests to the UniProt search tool (development only).",
            validation_mode=agentcore.PolicyValidationMode.IGNORE_ALL_FINDINGS,
        )

        # Blocks search_uniprot calls whose `species` parameter matches an entry on
        # BLOCKED_UNIPROT_SPECIES. The gateway flattens the tool's input schema
        # properties onto context.input, so this reads the same `species` field the
        # Lambda handler receives — no interceptor injection required.
        blocked_species_list = ", ".join(f'"{name}"' for name in BLOCKED_UNIPROT_SPECIES)
        policy_engine.add_policy(
            "ForbidBlockedUniProtSpecies",
            definition=(
                f'forbid(principal, action, resource == AgentCore::Gateway::"{gateway.gateway_arn}") '
                f'when {{ context has input && context.input has species && '
                f'[{blocked_species_list}].contains(context.input.species) }};'
            ),
            description="Block search_uniprot requests whose species parameter is on the blocked list.",
            validation_mode=agentcore.PolicyValidationMode.IGNORE_ALL_FINDINGS,
        )

        policy_engine.add_policy(
            "BiosafetyForbidMmseqs",
            definition=(
                f'forbid(principal, action, resource == AgentCore::Gateway::"{gateway.gateway_arn}") '
                f'when {{ context has input && context.input has _biosafety_mmseqs_risk_score && '
                f'context.input._biosafety_mmseqs_risk_score > {mmseqs_risk_threshold} }};'
            ),
            description="Block requests where MMseqs2 biosafety risk score exceeds threshold.",
            validation_mode=agentcore.PolicyValidationMode.IGNORE_ALL_FINDINGS,
        )
        policy_engine.add_policy(
            "BiosafetyForbidEmbedding",
            definition=(
                f'forbid(principal, action, resource == AgentCore::Gateway::"{gateway.gateway_arn}") '
                f'when {{ context has input && context.input has _biosafety_embedding_risk_score && '
                f'context.input._biosafety_embedding_risk_score > {embedding_risk_threshold} }};'
            ),
            description="Block requests where embedding biosafety risk score exceeds threshold.",
            validation_mode=agentcore.PolicyValidationMode.IGNORE_ALL_FINDINGS,
        )
        policy_engine.add_policy(
            "BiosafetyForbidFoldseek",
            definition=(
                f'forbid(principal, action, resource == AgentCore::Gateway::"{gateway.gateway_arn}") '
                f'when {{ context has input && context.input has _biosafety_foldseek_risk_score && '
                f'context.input._biosafety_foldseek_risk_score > {foldseek_risk_threshold} }};'
            ),
            description="Block requests where Foldseek structural biosafety risk score exceeds threshold.",
            validation_mode=agentcore.PolicyValidationMode.IGNORE_ALL_FINDINGS,
        )

        # Lambda target. The tool schema lives alongside the stack so it is
        # versioned with the code. Using add_lambda_target() is the
        # recommended pattern - it wires invoke permissions from the gateway
        # role to the Lambda automatically.
        tool_schema = agentcore.ToolSchema.from_local_asset(str(TOOL_SCHEMA_PATH))

        gateway.add_lambda_target(
            "SearchPmcLambdaTarget",
            gateway_target_name="pmc-search",
            description="Invokes the PMC search Lambda.",
            lambda_function=search_pmc_function,
            tool_schema=tool_schema,
        )

        # Placeholder Lambda target — replace with real implementation.
        ordering_tool_log_group = logs.LogGroup(
            self,
            "ExampleToolFunctionLogGroup",
            retention=LOG_RETENTION,
            removal_policy=RemovalPolicy.DESTROY,
        )

        ordering_tool_function = _lambda.Function(
            self,
            "ExampleToolFunction",
            function_name="ordering-tool",
            runtime=runtime,
            handler="research_gateway.tools.ordering.handler.handler",
            code=source_code,
            timeout=Duration.seconds(30),
            memory_size=128,
            log_group=ordering_tool_log_group,
            description="Placeholder for the ordering-tool gateway target.",
        )

        ordering_tool_schema = agentcore.ToolSchema.from_local_asset(
            str(ORDERING_TOOL_SCHEMA_PATH)
        )

        gateway.add_lambda_target(
            "ExampleToolLambdaTarget",
            gateway_target_name="ordering-tool",
            description="Placeholder ordering-tool target.",
            lambda_function=ordering_tool_function,
            tool_schema=ordering_tool_schema,
        )

        # UniProt protein search. Reuses the shared dependencies layer — the only
        # third-party import is httpx, which is already in layer/requirements.txt.
        uniprot_log_group = logs.LogGroup(
            self,
            "SearchUniProtFunctionLogGroup",
            retention=LOG_RETENTION,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # One search request plus one detail request per hit. The detail fetches are
        # fanned out concurrently, but 60s leaves headroom for a slow UniProt.
        uniprot_function = _lambda.Function(
            self,
            "SearchUniProtFunction",
            function_name="uniprot-search",
            runtime=runtime,
            handler="research_gateway.tools.uniprot.handler.handler",
            code=source_code,
            layers=[dependencies_layer],
            timeout=Duration.seconds(60),
            memory_size=512,
            log_group=uniprot_log_group,
            description="Searches UniProtKB and returns full protein detail records.",
        )

        uniprot_tool_schema = agentcore.ToolSchema.from_local_asset(
            str(UNIPROT_TOOL_SCHEMA_PATH)
        )

        gateway.add_lambda_target(
            "SearchUniProtLambdaTarget",
            gateway_target_name="uniprot-search",
            description="Invokes the UniProt protein search Lambda.",
            lambda_function=uniprot_function,
            tool_schema=uniprot_tool_schema,
        )

        gateway.add_interceptor(
            agentcore.LambdaInterceptor.for_request(interceptor_function)
        )

        # Tracing. AgentCore does not configure a span destination for gateways on
        # its own — the "Tracing" toggle in the AgentCore console creates a CloudWatch
        # vended-log delivery from the gateway to X-Ray, and these three resources are
        # that wiring. Spans land in the account-wide `aws/spans` log group and surface
        # on the CloudWatch GenAI Observability page.
        #
        # Prerequisite: CloudWatch Transaction Search must be enabled account-wide
        # (`aws xray update-trace-segment-destination --destination CloudWatchLogs`).
        # That is a one-time account-level setting shared with every other AgentCore
        # resource in the account, so it is deliberately not managed by this stack.
        traces_source_name = f"{gateway_name}-traces-source"
        traces_delivery_source = logs.CfnDeliverySource(
            self,
            "GatewayTracesDeliverySource",
            name=traces_source_name,
            log_type="TRACES",
            resource_arn=gateway.gateway_arn,
        )

        # XRAY destinations carry no destination_resource_arn — the span store is implicit.
        traces_delivery_destination = logs.CfnDeliveryDestination(
            self,
            "GatewayTracesDeliveryDestination",
            name=f"{gateway_name}-traces-destination",
            delivery_destination_type="XRAY",
        )

        traces_delivery = logs.CfnDelivery(
            self,
            "GatewayTracesDelivery",
            delivery_source_name=traces_source_name,
            delivery_destination_arn=traces_delivery_destination.attr_arn,
        )
        # delivery_source_name is a literal string, not a Ref, so CloudFormation cannot
        # infer this ordering on its own.
        traces_delivery.add_dependency(traces_delivery_source)

        # Application logs. Same three-resource delivery pattern as tracing, but the
        # destination is a CloudWatch log group rather than X-Ray. These carry the
        # per-request narrative — request/response bodies, target configuration errors,
        # rejected authorization headers, malformed tool names — and correlate to the
        # spans above via trace_id/span_id.
        #
        # CloudWatch requires the `/aws/vendedlogs/` prefix for vended log delivery. The
        # path is keyed on the gateway *name* rather than the gateway *id* (which is what
        # the AgentCore console uses) so that replacing the gateway keeps appending to one
        # log group instead of orphaning the old one and splitting the audit trail.
        application_logs_group = logs.LogGroup(
            self,
            "GatewayApplicationLogsGroup",
            log_group_name=(
                f"/aws/vendedlogs/bedrock-agentcore/gateway/APPLICATION_LOGS/{gateway_name}"
            ),
            retention=LOG_RETENTION,
            removal_policy=RemovalPolicy.DESTROY,
        )

        application_logs_source_name = f"{gateway_name}-app-logs-source"
        application_logs_delivery_source = logs.CfnDeliverySource(
            self,
            "GatewayApplicationLogsDeliverySource",
            name=application_logs_source_name,
            log_type="APPLICATION_LOGS",
            resource_arn=gateway.gateway_arn,
        )

        # destination_resource_arn takes the bare log group ARN. LogGroup.log_group_arn
        # ends in ":*", which this API rejects, so build the unsuffixed form.
        application_logs_delivery_destination = logs.CfnDeliveryDestination(
            self,
            "GatewayApplicationLogsDeliveryDestination",
            name=f"{gateway_name}-app-logs-destination",
            delivery_destination_type="CWL",
            destination_resource_arn=Stack.of(self).format_arn(
                service="logs",
                resource="log-group",
                resource_name=application_logs_group.log_group_name,
                arn_format=ArnFormat.COLON_RESOURCE_NAME,
            ),
        )

        application_logs_delivery = logs.CfnDelivery(
            self,
            "GatewayApplicationLogsDelivery",
            delivery_source_name=application_logs_source_name,
            delivery_destination_arn=application_logs_delivery_destination.attr_arn,
        )
        application_logs_delivery.add_dependency(application_logs_delivery_source)
        # destination_resource_arn is built from the log group *name*, not a GetAtt, so
        # nothing in the template orders these. The log group has to exist before the
        # destination points at it — and before the delivery, since creating the delivery
        # is what makes CloudWatch Logs attach the delivery.logs.amazonaws.com resource
        # policy to the group. Ordering the destination covers the delivery transitively.
        application_logs_delivery_destination.node.add_dependency(application_logs_group)

        CfnOutput(
            self,
            "PolicyEngineId",
            value=policy_engine.policy_engine_id,
            description="AgentCore PolicyEngine ID.",
        )
        CfnOutput(
            self,
            "GatewayId",
            value=gateway.gateway_id,
            description="AgentCore Gateway ID.",
        )
        CfnOutput(
            self,
            "GatewayUrl",
            value=gateway.gateway_url,
            description="AgentCore Gateway MCP endpoint URL.",
        )
        CfnOutput(
            self,
            "GatewayArn",
            value=gateway.gateway_arn,
            description=(
                "AgentCore Gateway ARN — use as the Resource in a caller's "
                "bedrock-agentcore:InvokeGateway policy."
            ),
        )
