# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Security regression tests for the container screening entry points."""

import importlib.util
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).parents[3]
CANONICAL_SEQUENCE = "ACDEFGHIKLMNPQRSTVWY"


def _load_module(relative_path: str):
    module_path = REPOSITORY_ROOT / relative_path
    spec = importlib.util.spec_from_file_location(
        f"security_test_{module_path.parent.name}", module_path
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "relative_path",
    [
        "deploy/containers/mmseqs2/handler.py",
        "deploy/containers/foldseek/inference.py",
    ],
)
def test_screeners_reject_non_amino_acid_input_before_subprocess(relative_path):
    module = _load_module(relative_path)

    assert module._validate_sequence(f"  {CANONICAL_SEQUENCE.lower()}  ") == CANONICAL_SEQUENCE
    with pytest.raises(ValueError, match="canonical amino-acid letters"):
        module.search(f"{CANONICAL_SEQUENCE}; touch /tmp/pwned")


def test_foldseek_uses_the_slim_python_base_image():
    dockerfile = REPOSITORY_ROOT / "deploy/containers/foldseek/Dockerfile"
    from_lines = [
        line
        for line in dockerfile.read_text().splitlines()
        if line.startswith("FROM ")
    ]
    assert from_lines == ["FROM public.ecr.aws/docker/library/python:3.12-slim"]
