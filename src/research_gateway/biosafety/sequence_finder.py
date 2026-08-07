# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import re
from typing import Any

_AA_RE = re.compile(r'[ACDEFGHIKLMNPQRSTVWY]{20,}')


def extract_sequences(obj: Any) -> list[str]:
    """Return deduplicated amino acid sequences found in obj (dict, list, or str)."""
    seen: dict[str, None] = {}  # ordered dict as ordered set
    _collect(obj, seen)
    return list(seen)


def _collect(obj: Any, seen: dict[str, None]) -> None:
    if isinstance(obj, str):
        for match in _AA_RE.findall(obj.upper()):
            seen[match] = None
    elif isinstance(obj, dict):
        for v in obj.values():
            _collect(v, seen)
    elif isinstance(obj, list):
        for item in obj:
            _collect(item, seen)
