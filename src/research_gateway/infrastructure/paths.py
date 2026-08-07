# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Centralized filesystem locations for infrastructure code."""

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
# The parent of the research_gateway package, not the package directory
# itself: `Code.from_asset` zips a directory's *contents* at the zip root, so
# this must be one level up from the package for `research_gateway.*`
# handler strings to resolve inside the deployed Lambda.
SOURCE_ROOT = PROJECT_ROOT / "src"
DEPLOY_ROOT = PROJECT_ROOT / "deploy"
CONTAINERS_ROOT = DEPLOY_ROOT / "containers"
LAYERS_ROOT = DEPLOY_ROOT / "layers"
SCHEMAS_ROOT = PROJECT_ROOT / "schemas" / "tools"
