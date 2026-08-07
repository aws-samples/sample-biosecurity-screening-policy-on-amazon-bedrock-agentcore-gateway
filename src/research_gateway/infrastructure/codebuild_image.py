# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""`CodeBuildDockerImage`: builds a Docker image on CodeBuild instead of the
local machine, and pushes it to a dedicated ECR repository.

Local `DockerImageAsset`/`DockerImageCode.from_image_asset` builds run
`docker build` wherever `cdk deploy` runs. For the ESMC/Foldseek/MMseqs2
containers -- multi-GB images baked with CUDA wheels, model weights, or
reference structure databases -- that means enough local disk and, on
non-x86_64 hosts, QEMU emulation for `platform=LINUX_AMD64`. This construct
moves the build to a privileged CodeBuild project running natively on
x86_64, and uses the CDK custom-resource Provider framework (see
`codebuild_trigger.py`) so CloudFormation waits for the build to finish
before anything that references the pushed image is created.
"""

from pathlib import Path

from aws_cdk import CustomResource, Duration, RemovalPolicy, Stack
from aws_cdk import aws_codebuild as codebuild
from aws_cdk import aws_ecr as ecr
from aws_cdk import aws_iam as iam
from aws_cdk import aws_s3_assets as s3_assets
from aws_cdk.custom_resources import Provider
from constructs import Construct

BUILD_TIMEOUT = Duration.minutes(45)


class CodeBuildDockerImage(Construct):
    """Builds `directory` as a Docker image on CodeBuild and pushes it to ECR.

    Exposes `.image_uri` (an `Fn::GetAtt` token -- use this, not a
    hand-composed f-string, so anything consuming it picks up an implicit
    CloudFormation dependency on the build finishing), `.image_tag` (the
    plain-string content hash, for callers like `DockerImageCode.from_ecr`
    that need a bare tag rather than a URI), `.repository`, and
    `.custom_resource` (for callers that must add an explicit dependency
    because they only take a plain string, not a token).
    """

    def __init__(
        self, scope: Construct, construct_id: str, *, directory: Path, provider: Provider
    ) -> None:
        super().__init__(scope, construct_id)
        stack = Stack.of(self)

        self.repository = ecr.Repository(
            self,
            "Repository",
            removal_policy=RemovalPolicy.DESTROY,
            empty_on_delete=True,
            lifecycle_rules=[
                ecr.LifecycleRule(
                    description="Expire untagged images left behind by failed or superseded builds.",
                    tag_status=ecr.TagStatus.UNTAGGED,
                    max_image_age=Duration.days(7),
                )
            ],
        )

        asset = s3_assets.Asset(self, "Source", path=str(directory))
        self.image_tag = asset.asset_hash

        self.project = codebuild.Project(
            self,
            "Project",
            source=codebuild.Source.s3(bucket=asset.bucket, path=asset.s3_object_key),
            environment=codebuild.BuildEnvironment(
                build_image=codebuild.LinuxBuildImage.STANDARD_7_0,
                compute_type=codebuild.ComputeType.LARGE,
                privileged=True,
            ),
            environment_variables={
                "ECR_REPO_URI": codebuild.BuildEnvironmentVariable(
                    value=self.repository.repository_uri
                ),
                "IMAGE_TAG": codebuild.BuildEnvironmentVariable(value=self.image_tag),
                "AWS_REGION": codebuild.BuildEnvironmentVariable(value=stack.region),
            },
            build_spec=codebuild.BuildSpec.from_object(
                {
                    "version": "0.2",
                    "phases": {
                        "pre_build": {
                            "commands": [
                                "aws ecr get-login-password --region $AWS_REGION | "
                                "docker login --username AWS --password-stdin $ECR_REPO_URI"
                            ]
                        },
                        "build": {
                            "commands": [
                                "docker build --platform linux/amd64 -t $ECR_REPO_URI:$IMAGE_TAG ."
                            ]
                        },
                        "post_build": {
                            "commands": ["docker push $ECR_REPO_URI:$IMAGE_TAG"]
                        },
                    },
                }
            ),
            timeout=BUILD_TIMEOUT,
        )
        asset.bucket.grant_read(self.project)

        # Push-only: no pull actions (BatchGetImage/GetDownloadUrlForLayer),
        # unlike AmazonEC2ContainerRegistryPowerUser, and scoped to this one
        # repository rather than the whole account.
        self.project.add_to_role_policy(
            iam.PolicyStatement(actions=["ecr:GetAuthorizationToken"], resources=["*"])
        )
        self.project.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "ecr:BatchCheckLayerAvailability",
                    "ecr:InitiateLayerUpload",
                    "ecr:UploadLayerPart",
                    "ecr:CompleteLayerUpload",
                    "ecr:PutImage",
                ],
                resources=[self.repository.repository_arn],
            )
        )

        self.custom_resource = CustomResource(
            self,
            "Trigger",
            service_token=provider.service_token,
            properties={
                "ProjectName": self.project.project_name,
                "ImageTag": self.image_tag,
                "RepositoryUri": self.repository.repository_uri,
            },
        )
        self.image_uri = self.custom_resource.get_att_string("ImageUri")
