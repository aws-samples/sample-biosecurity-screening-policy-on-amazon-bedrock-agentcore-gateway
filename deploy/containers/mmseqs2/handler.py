# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import csv
import os
import re
import subprocess
import tempfile

TARGET_DB = "/home/data/targetDB"
MAX_SEQUENCE_LENGTH = 10_000
_SEQUENCE_PATTERN = re.compile(r"^[ACDEFGHIKLMNPQRSTVWY]+$", re.IGNORECASE)

COLUMNS = [
    "query", "target", "pident", "alnlen", "mismatch", "gapopen",
    "qstart", "qend", "tstart", "tend", "evalue", "bits",
]


def _validate_sequence(sequence: str) -> str:
    """Return a normalized canonical amino-acid sequence or reject unsafe input."""
    if not isinstance(sequence, str):
        raise ValueError("event['sequence'] must be a string")

    normalized = sequence.strip().upper()
    if not normalized or len(normalized) > MAX_SEQUENCE_LENGTH:
        raise ValueError(f"event['sequence'] must contain 1-{MAX_SEQUENCE_LENGTH} amino acids")
    if not _SEQUENCE_PATTERN.fullmatch(normalized):
        raise ValueError("event['sequence'] must contain only canonical amino-acid letters")
    return normalized


def search(sequence: str) -> list[dict]:
    """Search one sequence in a private, automatically cleaned workspace."""
    sequence = _validate_sequence(sequence)
    with tempfile.TemporaryDirectory(prefix="mmseqs-") as work_dir:
        query_fasta = os.path.join(work_dir, "query.fasta")
        query_db = os.path.join(work_dir, "queryDB")
        result_db = os.path.join(work_dir, "resultDB")
        result_tsv = os.path.join(work_dir, "result.tsv")
        tmp_dir = os.path.join(work_dir, "tmp")
        os.makedirs(tmp_dir)

        with open(query_fasta, "w") as f:
            f.write(f">query\n{sequence}\n")

        # Each argument list below is a literal at the call site -- executables,
        # flags, and local temp-file paths only, never a shell, and the one
        # externally-influenced value (sequence) was already validated above
        # and never appears in these argv lists, only written into query.fasta.
        # check=True raises CalledProcessError (with captured stderr/stdout) on
        # a non-zero exit.
        subprocess.run(
            ["/usr/local/bin/mmseqs", "createdb", query_fasta, query_db],
            capture_output=True,
            text=True,
            cwd=work_dir,
            check=True,
        )
        subprocess.run(
            [
                "/usr/local/bin/mmseqs",
                "search",
                query_db,
                TARGET_DB,
                result_db,
                tmp_dir,
                "--threads",
                "1",
                "-s",
                "4.0",
                "--max-seqs",
                "10",
            ],
            capture_output=True,
            text=True,
            cwd=work_dir,
            check=True,
        )
        subprocess.run(
            [
                "/usr/local/bin/mmseqs",
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
            check=True,
        )

        with open(result_tsv) as f:
            reader = csv.DictReader(f, fieldnames=COLUMNS, delimiter="\t")
            return [
                {
                    **row,
                    "pident": float(row["pident"]),
                    "alnlen": int(row["alnlen"]),
                    "mismatch": int(row["mismatch"]),
                    "gapopen": int(row["gapopen"]),
                    "qstart": int(row["qstart"]),
                    "qend": int(row["qend"]),
                    "tstart": int(row["tstart"]),
                    "tend": int(row["tend"]),
                    "evalue": float(row["evalue"]),
                    "bits": float(row["bits"]),
                }
                for row in reader
            ]


def lambda_handler(event, _context):
    sequence = event.get("sequence")
    if not sequence or not isinstance(sequence, str):
        raise ValueError("event['sequence'] must be a non-empty string")

    hits = search(sequence)
    min_evalue = min((h["evalue"] for h in hits), default=None)
    return {"hits": hits, "count": len(hits), "min_evalue": min_evalue}
