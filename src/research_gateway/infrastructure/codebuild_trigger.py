# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""CDK custom-resource Provider handlers that drive a CodeBuild-based Docker
image build to completion.

Shared by every `research_gateway.infrastructure.codebuild_image.CodeBuildDockerImage`
instance via one `custom_resources.Provider`. `on_event` starts the build;
`is_complete` polls it. Per the Provider framework contract, any field other
than `PhysicalResourceId`/`Data`/`NoEcho` that `on_event` returns is passed
through verbatim to `is_complete` -- `BuildId` here -- while `Data` is only
for the final `Fn::GetAtt`-visible attributes (`ImageUri`), which is why the
two are kept separate below rather than folding `BuildId` into `Data`.
"""

import boto3

_codebuild_client = boto3.client("codebuild")

TERMINAL_FAILURE_STATUSES = {"FAILED", "FAULT", "STOPPED", "TIMED_OUT"}


def on_event(event, _context):
    if event["RequestType"] == "Delete":
        return {"PhysicalResourceId": event["PhysicalResourceId"]}

    props = event["ResourceProperties"]
    project_name = props["ProjectName"]
    image_tag = props["ImageTag"]
    repository_uri = props["RepositoryUri"]

    build = _codebuild_client.start_build(projectName=project_name)["build"]

    return {
        "PhysicalResourceId": f"{project_name}/{image_tag}",
        "BuildId": build["id"],
        "Data": {"ImageUri": f"{repository_uri}:{image_tag}"},
    }


def is_complete(event, _context):
    if event["RequestType"] == "Delete":
        return {"IsComplete": True}

    build_id = event["BuildId"]
    builds = _codebuild_client.batch_get_builds(ids=[build_id])["builds"]
    if not builds:
        # CodeBuild is eventually consistent immediately after StartBuild.
        return {"IsComplete": False}

    status = builds[0]["buildStatus"]
    if status == "SUCCEEDED":
        return {"IsComplete": True}
    if status in TERMINAL_FAILURE_STATUSES:
        raise RuntimeError(f"CodeBuild build {build_id} ended with status {status}")
    return {"IsComplete": False}
