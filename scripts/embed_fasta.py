#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Embed all sequences in a FASTA file using the ESMC-600M SageMaker endpoint.

Reads sequences from a FASTA file, fetches a mean-pooled embedding for each one
from the endpoint, and saves the results to a .npz file suitable for semantic
search with numpy/faiss/sklearn.

The endpoint takes ``{"sequence": ...}`` and returns ``{"embedding": [...]}`` —
tokenization, BOS/EOS stripping and mean-pooling all happen inside the container,
so this script does one call per sequence.

This must be run against the same endpoint that serves queries at runtime: the
reference index and the query path have to come from an identical code path or
cosine scores — and the risk thresholds calibrated against them — are not
comparable.

Output .npz contains:
  ids          str array  (N,)       sequence identifiers
  embeddings   float32    (N, D)     L2-normalised mean per-sequence embeddings

Writes incrementally after each batch so progress is never lost on interruption.
Re-running automatically resumes from where it left off.

Usage:
    python embed_fasta.py --fasta ../deploy/containers/mmseqs2/SafeProtein_Bench.fasta
    python embed_fasta.py --fasta seqs.fasta --output embeddings.npz
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import time
from pathlib import Path
from typing import Generator

import boto3
import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FASTA = PROJECT_ROOT / "deploy" / "containers" / "mmseqs2" / "SafeProtein_Bench.fasta"
DEFAULT_OUTPUT = (
    PROJECT_ROOT
    / "src"
    / "research_gateway"
    / "screening"
    / "embedding"
    / "data"
    / "safeprotein_bench_index.npz"
)

ENDPOINT_NAME = "esmc-600m"
EMBEDDING_DIM = 1152

# Sequences are embedded one per request; this only controls how often progress
# is checkpointed to disk.
DEFAULT_BATCH_SIZE = 32

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# FASTA parsing
# ---------------------------------------------------------------------------


def parse_fasta(path: Path) -> Generator[tuple[str, str], None, None]:
    seq_id = None
    chunks: list[str] = []
    with path.open() as f:
        for line in f:
            line = line.rstrip()
            if line.startswith(">"):
                if seq_id is not None:
                    yield seq_id, "".join(chunks)
                seq_id = line[1:].split()[0]
                chunks = []
            elif line:
                chunks.append(line)
    if seq_id is not None:
        yield seq_id, "".join(chunks)


def make_batches(
    sequences: list[tuple[str, str]],
    max_size: int,
) -> list[list[tuple[str, str]]]:
    """Group sequences into fixed-size chunks, one checkpoint per chunk."""
    return [sequences[i : i + max_size] for i in range(0, len(sequences), max_size)]


# ---------------------------------------------------------------------------
# Incremental checkpoint writer
# ---------------------------------------------------------------------------


def save_checkpoint(
    output_path: Path,
    ids: list[str],
    embeddings: list[list[float]],
) -> None:
    """Atomically write ids + L2-normalised embeddings to output_path via a temp file."""
    arr = np.array(embeddings, dtype=np.float32)
    norms = np.linalg.norm(arr, axis=1, keepdims=True) + 1e-10
    arr = arr / norms
    tmp = output_path.with_suffix(".tmp.npz")
    np.savez(tmp, ids=np.array(ids), embeddings=arr)
    os.replace(tmp, output_path)


def load_checkpoint(output_path: Path) -> tuple[list[str], list[list[float]]]:
    """Load existing ids and embeddings from a checkpoint file."""
    data = np.load(output_path, allow_pickle=True)
    return list(data["ids"]), data["embeddings"].tolist()


# ---------------------------------------------------------------------------
# SageMaker client
# ---------------------------------------------------------------------------


class ESMCSageMakerClient:
    def __init__(self, endpoint_name: str, region: str | None = None):
        self.endpoint_name = endpoint_name
        self._client = boto3.client("sagemaker-runtime", region_name=region)

    def embed(self, sequence: str) -> list[float]:
        """Return the mean-pooled embedding the endpoint computes for a sequence."""
        try:
            response = self._client.invoke_endpoint(
                EndpointName=self.endpoint_name,
                ContentType="application/json",
                Body=json.dumps({"sequence": sequence}),
            )
        except Exception as e:
            raise RuntimeError(f"Endpoint call failed: {e}")

        data = json.loads(response["Body"].read().decode())
        raw = data.get("embedding")
        if not raw:
            raise ValueError("No embedding in response")

        embedding = np.array(raw, dtype=np.float32)
        if embedding.ndim != 1 or embedding.shape[0] != EMBEDDING_DIM:
            raise ValueError(
                f"Expected a {EMBEDDING_DIM}-d embedding, got shape {embedding.shape}"
            )
        return embedding.tolist()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="Embed a FASTA file with ESMC-600M.")
    parser.add_argument(
        "--fasta",
        default=str(DEFAULT_FASTA),
        help=f"Path to input FASTA file (default: {DEFAULT_FASTA}).",
    )
    parser.add_argument(
        "--output",
        default=str(DEFAULT_OUTPUT),
        help=f"Output .npz file (default: {DEFAULT_OUTPUT}).",
    )
    parser.add_argument("--endpoint-name", default=ENDPOINT_NAME)
    parser.add_argument("--region", default=None)
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help="Sequences per checkpoint write (default: 32).",
    )
    parser.add_argument("--retry-delay", type=float, default=5.0)
    parser.add_argument("--max-retries", type=int, default=3)
    args = parser.parse_args()

    fasta_path = Path(args.fasta)
    output_path = Path(args.output)

    if not fasta_path.exists():
        raise FileNotFoundError(f"FASTA file not found: {fasta_path}")

    all_sequences = list(parse_fasta(fasta_path))
    log.info("Loaded %d sequences from %s", len(all_sequences), fasta_path)

    # Always resume if checkpoint exists
    ids: list[str] = []
    embeddings: list[list[float]] = []
    done_ids: set[str] = set()
    if output_path.exists():
        ids, embeddings = load_checkpoint(output_path)
        done_ids = set(ids)
        log.info("Resuming from checkpoint: %d sequences already done", len(done_ids))

    todo = [(sid, seq) for sid, seq in all_sequences if sid not in done_ids]
    log.info("%d sequences remaining", len(todo))

    if not todo:
        log.info("Nothing to do.")
        return

    region = args.region or boto3.session.Session().region_name or "us-east-1"
    client = ESMCSageMakerClient(endpoint_name=args.endpoint_name, region=region)

    batches = make_batches(todo, args.batch_size)
    log.info("Processing in %d batches", len(batches))

    for batch_idx, batch in enumerate(batches):
        log.info("Batch %d/%d  (%d sequences)", batch_idx + 1, len(batches), len(batch))

        for seq_id, seq in batch:
            for attempt in range(1, args.max_retries + 1):
                try:
                    embedding = client.embed(seq)
                    ids.append(seq_id)
                    embeddings.append(embedding)
                    break
                except Exception as e:
                    if attempt == args.max_retries:
                        log.error(
                            "Skipping %s after %d attempts: %s",
                            seq_id,
                            args.max_retries,
                            e,
                        )
                    else:
                        log.warning("Retry %d for %s: %s", attempt, seq_id, e)
                        time.sleep(args.retry_delay * attempt)

        # Write after every batch so progress survives interruption
        save_checkpoint(output_path, ids, embeddings)
        log.info("Checkpoint saved: %d/%d sequences", len(ids), len(all_sequences))

    log.info(
        "Done. Saved %d embeddings (shape %s) to %s",
        len(ids),
        np.array(embeddings, dtype=np.float32).shape,
        output_path,
    )


if __name__ == "__main__":
    main()
