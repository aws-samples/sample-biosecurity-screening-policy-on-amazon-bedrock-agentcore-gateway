# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Tests for research_gateway.infrastructure.codebuild_trigger -- the CDK
custom-resource Provider handlers that start and poll a CodeBuildDockerImage
build.

boto3's codebuild client is created at module import time, so each test
replaces it with a fake via monkeypatch.setattr, matching the pattern used for
research_gateway.biosafety.interceptor's module-level clients.
"""

import pytest

from research_gateway.infrastructure import codebuild_trigger


class _FakeCodeBuildClient:
    def __init__(self):
        self.start_build_calls = []
        self.batch_get_builds_calls = []
        self.build_id = "arn:aws:codebuild:us-east-1:111111111111:build/esmc:abc123"
        self.build_status = "SUCCEEDED"

    def start_build(self, projectName):  # noqa: N803
        self.start_build_calls.append(projectName)
        return {"build": {"id": self.build_id}}

    def batch_get_builds(self, ids):
        self.batch_get_builds_calls.append(ids)
        if self.build_status is None:
            return {"builds": []}
        return {"builds": [{"id": ids[0], "buildStatus": self.build_status}]}


@pytest.fixture
def fake_client(monkeypatch):
    client = _FakeCodeBuildClient()
    monkeypatch.setattr(codebuild_trigger, "_codebuild_client", client)
    return client


def _create_event(request_type="Create"):
    return {
        "RequestType": request_type,
        "ResourceProperties": {
            "ProjectName": "EsmcProject",
            "ImageTag": "abc123",
            "RepositoryUri": "111111111111.dkr.ecr.us-east-1.amazonaws.com/esmc",
        },
    }


# ---------------------------------------------------------------------------
# on_event
# ---------------------------------------------------------------------------


def test_on_event_create_starts_build_and_returns_image_uri(fake_client):
    result = codebuild_trigger.on_event(_create_event("Create"), None)

    assert fake_client.start_build_calls == ["EsmcProject"]
    assert result["PhysicalResourceId"] == "EsmcProject/abc123"
    assert result["BuildId"] == fake_client.build_id
    assert result["Data"] == {
        "ImageUri": "111111111111.dkr.ecr.us-east-1.amazonaws.com/esmc:abc123"
    }


def test_on_event_update_starts_a_new_build(fake_client):
    event = _create_event("Update")
    event["PhysicalResourceId"] = "EsmcProject/oldtag"
    result = codebuild_trigger.on_event(event, None)

    assert fake_client.start_build_calls == ["EsmcProject"]
    # New tag from ResourceProperties wins -- this is what makes CloudFormation
    # trigger a rebuild only when the source content hash actually changed.
    assert result["PhysicalResourceId"] == "EsmcProject/abc123"


def test_on_event_delete_does_not_start_a_build(fake_client):
    event = {"RequestType": "Delete", "PhysicalResourceId": "EsmcProject/abc123"}
    result = codebuild_trigger.on_event(event, None)

    assert fake_client.start_build_calls == []
    assert result == {"PhysicalResourceId": "EsmcProject/abc123"}


# ---------------------------------------------------------------------------
# is_complete
# ---------------------------------------------------------------------------


def _complete_event(build_id="build-1", request_type="Create"):
    return {"RequestType": request_type, "BuildId": build_id}


def test_is_complete_true_on_succeeded(fake_client):
    fake_client.build_status = "SUCCEEDED"
    result = codebuild_trigger.is_complete(_complete_event(), None)
    assert result == {"IsComplete": True}


@pytest.mark.parametrize("status", ["FAILED", "FAULT", "STOPPED", "TIMED_OUT"])
def test_is_complete_raises_on_terminal_failure_statuses(fake_client, status):
    fake_client.build_status = status
    with pytest.raises(RuntimeError, match=status):
        codebuild_trigger.is_complete(_complete_event(), None)


def test_is_complete_false_while_in_progress(fake_client):
    fake_client.build_status = "IN_PROGRESS"
    result = codebuild_trigger.is_complete(_complete_event(), None)
    assert result == {"IsComplete": False}


def test_is_complete_false_when_build_not_yet_visible(fake_client):
    fake_client.build_status = None
    result = codebuild_trigger.is_complete(_complete_event(), None)
    assert result == {"IsComplete": False}


def test_is_complete_delete_is_always_complete(fake_client):
    result = codebuild_trigger.is_complete(_complete_event(request_type="Delete"), None)
    assert result == {"IsComplete": True}
    assert fake_client.batch_get_builds_calls == []
