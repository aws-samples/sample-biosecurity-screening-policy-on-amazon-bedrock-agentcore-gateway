# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import csv
import os
import re
import subprocess
import tempfile
import time

TARGET_DB = "/opt/ml/data/targetDB"
PROSTT5_WEIGHTS = "/opt/ml/data/prostt5_weights/prostt5-f16.gguf"
MAX_SEQUENCE_LENGTH = 10_000
_SEQUENCE_PATTERN = re.compile(r"^[ACDEFGHIKLMNPQRSTVWY]+$", re.IGNORECASE)

COLUMNS = ["query", "target", "fident", "alnlen", "evalue", "bits"]


def _validate_sequence(sequence: str) -> str:
    """Return a normalized canonical amino-acid sequence or reject unsafe input."""
    if not isinstance(sequence, str):
        raise ValueError("sequence must be a string")

    normalized = sequence.strip().upper()
    if not normalized or len(normalized) > MAX_SEQUENCE_LENGTH:
        raise ValueError(f"sequence must contain 1-{MAX_SEQUENCE_LENGTH} amino acids")
    if not _SEQUENCE_PATTERN.fullmatch(normalized):
        raise ValueError("sequence must contain only canonical amino-acid letters")
    return normalized


def _check(result: subprocess.CompletedProcess, step: str, elapsed: float) -> None:
    # Timed and printed (SageMaker ships container stdout to CloudWatch) so a
    # future latency regression is diagnosable per-step instead of opaque.
    print(f"foldseek {step}: finished in {elapsed:.1f}s (returncode={result.returncode})")
    if result.returncode != 0:
        raise RuntimeError(f"foldseek {step} failed: stderr: {result.stderr}\nstdout: {result.stdout}")


def search(sequence: str) -> list[dict]:
    """Search one sequence in a private, automatically cleaned workspace."""
    sequence = _validate_sequence(sequence)
    with tempfile.TemporaryDirectory(prefix="foldseek-") as work_dir:
        query_fasta = os.path.join(work_dir, "query.fasta")
        query_db = os.path.join(work_dir, "queryDB")
        result_db = os.path.join(work_dir, "resultDB")
        result_tsv = os.path.join(work_dir, "results.m8")
        tmp_dir = os.path.join(work_dir, "tmp")
        os.makedirs(tmp_dir)

        with open(query_fasta, "w") as f:
            f.write(f">query\n{sequence.upper()}\n")

        # Each argument list below is a literal at the call site -- executables,
        # flags, and local temp-file paths only, never a shell, and the one
        # externally-influenced value (sequence) was already validated above
        # and never appears in these argv lists, only written into query.fasta.
        start = time.perf_counter()
        result = subprocess.run(
            [
                "/usr/local/bin/foldseek",
                "createdb",
                query_fasta,
                query_db,
                "--prostt5-model",
                PROSTT5_WEIGHTS,
            ],
            capture_output=True,
            text=True,
            cwd=work_dir,
        )
        _check(result, "createdb (ProstT5)", time.perf_counter() - start)

        start = time.perf_counter()
        result = subprocess.run(
            [
                "/usr/local/bin/foldseek",
                "search",
                query_db,
                TARGET_DB,
                result_db,
                tmp_dir,
                "--prefilter-mode",
                "1",
                "--threads",
                "1",
                "--max-seqs",
                "10",
            ],
            capture_output=True,
            text=True,
            cwd=work_dir,
        )
        _check(result, "search", time.perf_counter() - start)

        start = time.perf_counter()
        result = subprocess.run(
            [
                "/usr/local/bin/foldseek",
                "convertalis",
                query_db,
                TARGET_DB,
                result_db,
                result_tsv,
                "--format-output",
                ",".join(COLUMNS),
            ],
            capture_output=True,
            text=True,
            cwd=work_dir,
        )
        _check(result, "convertalis", time.perf_counter() - start)

        with open(result_tsv) as f:
            reader = csv.DictReader(f, fieldnames=COLUMNS, delimiter="\t")
            return [
                {
                    **row,
                    "fident": float(row["fident"]),
                    "alnlen": int(row["alnlen"]),
                    "evalue": float(row["evalue"]),
                    "bits": float(row["bits"]),
                }
                for row in reader
            ]
