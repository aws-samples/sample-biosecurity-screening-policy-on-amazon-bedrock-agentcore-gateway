# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""CDK stack that deploys all biosafety and biosecurity resources:

- ESMC-600M SageMaker real-time inference endpoint (self-built container serving
  the open-source `biohub/ESMC-600M` weights; see `esmc/Dockerfile`)
- MMseqs2 Docker Lambda for protein sequence alignment against a threat database
- Embedding-screening Lambda for cosine-similarity search via ESMC-600M embeddings
- Biosafety interceptor Lambda that screens gateway requests and enriches context

Also owns the biosafety Cedar policy definition, which is passed to the gateway
stack so all biosafety concerns are co-located here.
"""

from aws_cdk import CfnOutput, Duration, Size, Stack
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as _lambda
from aws_cdk import aws_sagemaker as sagemaker
from aws_cdk.aws_lambda_python_alpha import PythonLayerVersion
from aws_cdk.custom_resources import Provider
from constructs import Construct

from research_gateway.infrastructure.codebuild_image import CodeBuildDockerImage
from research_gateway.infrastructure.paths import CONTAINERS_ROOT, LAYERS_ROOT, SOURCE_ROOT

EMBEDDING_SCREENING_LAYER_DIR = LAYERS_ROOT / "embedding"
ESMC_DIR = CONTAINERS_ROOT / "esmc"
FOLDSEEK_DIR = CONTAINERS_ROOT / "foldseek"
MMSEQS_DIR = CONTAINERS_ROOT / "mmseqs2"

DEFAULT_MMSEQS_RISK_THRESHOLD = 5
DEFAULT_EMBEDDING_RISK_THRESHOLD = 95
DEFAULT_FOLDSEEK_RISK_THRESHOLD = 5
ESMC_ENDPOINT_NAME = "esmc-600m"
# A10G/24 GB is ample for a 600M model doing single-sequence inference.
ESMC_DEFAULT_INSTANCE_TYPE = "ml.g5.xlarge"
ESMC_DEFAULT_INSTANCE_COUNT = 1
# The ~7 GB image is pulled before container startup, and loading 2.3 GB of
# weights onto the GPU takes tens of seconds. The default startup health-check
# budget is 480s; relax it so a slow first pull doesn't fail CreateEndpoint.
ESMC_STARTUP_HEALTH_CHECK_TIMEOUT = 900
FOLDSEEK_ENDPOINT_NAME = "foldseek-prostt5"
# ml.c6i.2xlarge (8 dedicated Ice Lake vCPUs) on a CPU-only image -- confirmed via
# CloudWatch ModelLatency at 1.0-2.3s across real invocations, matching the prior
# g5.xlarge deployment's latency at lower cost, since no GPU hardware is provisioned.
FOLDSEEK_DEFAULT_INSTANCE_TYPE = "ml.c6i.2xlarge"
FOLDSEEK_DEFAULT_INSTANCE_COUNT = 1
# The image is still several GB (CPU foldseek binary + ProstT5 weights + threat
# DB); keep headroom for a cold first pull rather than tuning this down
# opportunistically.
FOLDSEEK_STARTUP_HEALTH_CHECK_TIMEOUT = 900


class BiosafetyStack(Stack):
    """Deploys all biosafety and biosecurity resources.

    Exposes interceptor_function and forbid_cedar_policy for the gateway stack.
    """

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.mmseqs_risk_threshold = int(
            self.node.try_get_context("mmseqs_risk_threshold") or DEFAULT_MMSEQS_RISK_THRESHOLD
        )
        self.embedding_risk_threshold = int(
            self.node.try_get_context("embedding_risk_threshold") or DEFAULT_EMBEDDING_RISK_THRESHOLD
        )
        self.foldseek_risk_threshold = int(
            self.node.try_get_context("foldseek_risk_threshold") or DEFAULT_FOLDSEEK_RISK_THRESHOLD
        )

        # ── Shared source asset + CodeBuild image-trigger Provider ─────────

        # Shared source asset for every Python Lambda in this stack -- the whole
        # research_gateway package, addressed by package-qualified handler paths.
        source_code = _lambda.Code.from_asset(str(SOURCE_ROOT))

        codebuild_on_event_fn = _lambda.Function(
            self,
            "CodeBuildOnEventFunction",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="research_gateway.infrastructure.codebuild_trigger.on_event",
            code=source_code,
            architecture=_lambda.Architecture.X86_64,
            timeout=Duration.seconds(30),
            description="Starts a CodeBuild image build on behalf of a CodeBuildDockerImage custom resource.",
        )
        codebuild_is_complete_fn = _lambda.Function(
            self,
            "CodeBuildIsCompleteFunction",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="research_gateway.infrastructure.codebuild_trigger.is_complete",
            code=source_code,
            architecture=_lambda.Architecture.X86_64,
            timeout=Duration.seconds(30),
            description="Polls a CodeBuildDockerImage build until it succeeds or fails.",
        )
        codebuild_image_provider = Provider(
            self,
            "CodeBuildImageProvider",
            on_event_handler=codebuild_on_event_fn,
            is_complete_handler=codebuild_is_complete_fn,
            query_interval=Duration.seconds(30),
            total_timeout=Duration.hours(1),
        )

        # ── ESMC-600M SageMaker endpoint ────────────────────────────────────

        instance_type = (
            self.node.try_get_context("esmc_instance_type") or ESMC_DEFAULT_INSTANCE_TYPE
        )
        instance_count = int(
            self.node.try_get_context("esmc_instance_count") or ESMC_DEFAULT_INSTANCE_COUNT
        )

        esmc_execution_role = iam.Role(
            self,
            "EsmcSageMakerExecutionRole",
            assumed_by=iam.ServicePrincipal("sagemaker.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name("AmazonSageMakerFullAccess"),
            ],
            description="Execution role for the ESMC-600M SageMaker endpoint.",
        )

        esmc_image = CodeBuildDockerImage(
            self, "EsmcImage", directory=ESMC_DIR, provider=codebuild_image_provider
        )

        # Network isolation blocks the container's own outbound calls, which is
        # safe here because the weights are baked into the image. SageMaker pulls
        # the ECR image with the execution role, in isolation from the container,
        # so the pull itself is unaffected.
        esmc_model = sagemaker.CfnModel(
            self,
            "EsmcModel",
            execution_role_arn=esmc_execution_role.role_arn,
            enable_network_isolation=True,
            primary_container=sagemaker.CfnModel.ContainerDefinitionProperty(
                image=esmc_image.image_uri,
            ),
        )

        esmc_endpoint_config = sagemaker.CfnEndpointConfig(
            self,
            "EsmcEndpointConfig",
            production_variants=[
                sagemaker.CfnEndpointConfig.ProductionVariantProperty(
                    model_name=esmc_model.attr_model_name,
                    variant_name="AllTraffic",
                    initial_instance_count=instance_count,
                    instance_type=instance_type,
                    initial_variant_weight=1,
                    container_startup_health_check_timeout_in_seconds=(
                        ESMC_STARTUP_HEALTH_CHECK_TIMEOUT
                    ),
                )
            ],
        )

        sagemaker.CfnEndpoint(
            self,
            "EsmcEndpoint",
            endpoint_name=ESMC_ENDPOINT_NAME,
            endpoint_config_name=esmc_endpoint_config.attr_endpoint_config_name,
        )

        CfnOutput(
            self,
            "EsmcEndpointName",
            value=ESMC_ENDPOINT_NAME,
            description="ESMC-600M SageMaker endpoint name.",
        )
        CfnOutput(
            self,
            "EsmcEndpointUrl",
            value=f"https://runtime.sagemaker.{self.region}.amazonaws.com/endpoints/{ESMC_ENDPOINT_NAME}/invocations",
            description="ESMC-600M SageMaker endpoint invocation URL.",
        )

        # ── Foldseek + ProstT5 SageMaker endpoint ───────────────────────────

        foldseek_instance_type = (
            self.node.try_get_context("foldseek_instance_type") or FOLDSEEK_DEFAULT_INSTANCE_TYPE
        )
        foldseek_instance_count = int(
            self.node.try_get_context("foldseek_instance_count") or FOLDSEEK_DEFAULT_INSTANCE_COUNT
        )

        foldseek_execution_role = iam.Role(
            self,
            "FoldseekSageMakerExecutionRole",
            assumed_by=iam.ServicePrincipal("sagemaker.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name("AmazonSageMakerFullAccess"),
            ],
            description="Execution role for the Foldseek+ProstT5 SageMaker endpoint.",
        )

        foldseek_image = CodeBuildDockerImage(
            self, "FoldseekImage", directory=FOLDSEEK_DIR, provider=codebuild_image_provider
        )

        foldseek_model = sagemaker.CfnModel(
            self,
            "FoldseekModel",
            execution_role_arn=foldseek_execution_role.role_arn,
            primary_container=sagemaker.CfnModel.ContainerDefinitionProperty(
                image=foldseek_image.image_uri,
                environment={
                    "SAGEMAKER_PROGRAM": "inference.py",
                },
            ),
        )

        foldseek_endpoint_config = sagemaker.CfnEndpointConfig(
            self,
            "FoldseekEndpointConfig",
            production_variants=[
                sagemaker.CfnEndpointConfig.ProductionVariantProperty(
                    model_name=foldseek_model.attr_model_name,
                    variant_name="AllTraffic",
                    initial_instance_count=foldseek_instance_count,
                    instance_type=foldseek_instance_type,
                    initial_variant_weight=1,
                    container_startup_health_check_timeout_in_seconds=(
                        FOLDSEEK_STARTUP_HEALTH_CHECK_TIMEOUT
                    ),
                )
            ],
        )

        sagemaker.CfnEndpoint(
            self,
            "FoldseekEndpoint",
            endpoint_name=FOLDSEEK_ENDPOINT_NAME,
            endpoint_config_name=foldseek_endpoint_config.attr_endpoint_config_name,
        )

        CfnOutput(
            self,
            "FoldseekEndpointName",
            value=FOLDSEEK_ENDPOINT_NAME,
            description="Foldseek+ProstT5 SageMaker endpoint name.",
        )

        # ── MMseqs2 screening Lambda ─────────────────────────────────────────

        mmseqs_image = CodeBuildDockerImage(
            self, "MmseqsImage", directory=MMSEQS_DIR, provider=codebuild_image_provider
        )

        self.screening_function = _lambda.DockerImageFunction(
            self,
            "MmseqsFunction",
            function_name="mmseqs-screening",
            code=_lambda.DockerImageCode.from_ecr(
                repository=mmseqs_image.repository, tag_or_digest=mmseqs_image.image_tag
            ),
            architecture=_lambda.Architecture.X86_64,
            memory_size=3008,
            ephemeral_storage_size=Size.mebibytes(2048),
            timeout=Duration.seconds(15),
            description="MMseqs2 protein sequence screening against threat database.",
        )
        # DockerImageCode.from_ecr takes a plain tag string, not the
        # Fn::GetAtt-backed `.image_uri` token, so unlike EsmcModel/FoldseekModel
        # above this needs an explicit dependency to make CloudFormation wait
        # for the push to finish before creating the function from that tag.
        self.screening_function.node.add_dependency(mmseqs_image.custom_resource)

        for image in (esmc_image, foldseek_image, mmseqs_image):
            codebuild_on_event_fn.add_to_role_policy(
                iam.PolicyStatement(
                    actions=["codebuild:StartBuild"], resources=[image.project.project_arn]
                )
            )
            codebuild_is_complete_fn.add_to_role_policy(
                iam.PolicyStatement(
                    actions=["codebuild:BatchGetBuilds"], resources=[image.project.project_arn]
                )
            )

        # ── Embedding screening Lambda ───────────────────────────────────────

        embedding_layer = PythonLayerVersion(
            self,
            "EmbeddingScreeningLayer",
            entry=str(EMBEDDING_SCREENING_LAYER_DIR),
            compatible_runtimes=[_lambda.Runtime.PYTHON_3_12],
            description="numpy dependency for the embedding-screening Lambda.",
        )

        self.embedding_screening_function = _lambda.Function(
            self,
            "EmbeddingScreeningFunction",
            function_name="embedding-screening",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="research_gateway.screening.embedding.handler.lambda_handler",
            code=source_code,
            layers=[embedding_layer],
            architecture=_lambda.Architecture.X86_64,
            memory_size=512,
            timeout=Duration.seconds(30),
            environment={
                "SAGEMAKER_ENDPOINT_NAME": ESMC_ENDPOINT_NAME,
                "TOP_K": "10",
            },
            description="Embedding-based protein screening using ESMC-600M cosine similarity.",
        )

        self.embedding_screening_function.add_to_role_policy(
            iam.PolicyStatement(
                actions=["sagemaker:InvokeEndpoint"],
                resources=[
                    f"arn:aws:sagemaker:{Stack.of(self).region}:{Stack.of(self).account}:endpoint/{ESMC_ENDPOINT_NAME}"
                ],
            )
        )

        # ── Biosafety interceptor Lambda ─────────────────────────────────────

        self.interceptor_function = _lambda.Function(
            self,
            "BiosafetyInterceptorFunction",
            function_name="biosafety-interceptor",
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="research_gateway.biosafety.interceptor.lambda_handler",
            code=source_code,
            architecture=_lambda.Architecture.X86_64,
            memory_size=256,
            timeout=Duration.seconds(60),
            environment={
                "SCREENING_FUNCTION_NAME": self.screening_function.function_name,
                "EMBEDDING_SCREENING_FUNCTION_NAME": self.embedding_screening_function.function_name,
                "FOLDSEEK_ENDPOINT_NAME": FOLDSEEK_ENDPOINT_NAME,
            },
            description="Intercepts gateway requests to screen sequences for biosafety.",
        )

        self.screening_function.grant_invoke(self.interceptor_function)
        self.embedding_screening_function.grant_invoke(self.interceptor_function)

        self.interceptor_function.add_to_role_policy(
            iam.PolicyStatement(
                actions=["sagemaker:InvokeEndpoint"],
                resources=[
                    f"arn:aws:sagemaker:{Stack.of(self).region}:{Stack.of(self).account}:endpoint/{FOLDSEEK_ENDPOINT_NAME}"
                ],
            )
        )
