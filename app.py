#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""CDK application entrypoint for the Research Gateway stacks."""

from research_gateway.infrastructure.application import synth_app

synth_app()
