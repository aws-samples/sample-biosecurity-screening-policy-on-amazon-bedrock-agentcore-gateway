# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Lambda handler for embedding-based protein sequence screening.

Embeds a query sequence using the ESMC-600M SageMaker endpoint and returns
the top-k most similar sequences from the pre-computed SafeProtein_Bench
reference index, along with a maximum cosine similarity score.

The endpoint takes ``{"sequence": ...}`` and returns a mean-pooled 1152-d
embedding as ``{"embedding": [...]}`` — tokenization and mean-pooling happen
inside the container.

Input:  {"sequence": "<amino_acid_string>"}
Output: {"hits": [{"id": "...", "similarity": 0.97}], "count": N, "max_similarity": 0.97}
"""

import json
import logging
import os
from importlib import resources  # nosemgrep: python.lang.compatibility.python37.python37-compatibility-importlib2 -- Lambda runtime is pinned to Python 3.12 (pyproject.toml requires-python), so pre-3.7 compatibility is not a project requirement.

import boto3
import numpy as np

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

SAGEMAKER_ENDPOINT_NAME = os.environ.get("SAGEMAKER_ENDPOINT_NAME", "esmc-600m")
TOP_K = int(os.environ.get("TOP_K", "10"))
EMBEDDING_DIM = 1152

# Load the pre-normalised reference index once at cold start (~2 MB). Resolved via
# importlib.resources rather than __file__ so it works regardless of the working
# directory or how the package was installed.
_INDEX_TRAVERSABLE = resources.files("research_gateway.screening.embedding").joinpath(
    "data", "safeprotein_bench_index.npz"
)
with resources.as_file(_INDEX_TRAVERSABLE) as _index_path:
    _data = np.load(_index_path, allow_pickle=True)
_REF_IDS: np.ndarray = _data["ids"]                              # shape (N,)
_REF_EMBEDDINGS: np.ndarray = _data["embeddings"].astype(np.float32)  # shape (N, 1152), L2-normalised

_sm_client = boto3.client("sagemaker-runtime")


def _embed_sequence(sequence: str) -> np.ndarray:
    """Return a mean-pooled float32 embedding vector (shape 1152,) for a protein sequence.

    The endpoint tokenizes, runs the model, strips BOS/EOS and mean-pools the
    per-residue embeddings server-side, returning the final vector directly.
    """
    try:
        response = _sm_client.invoke_endpoint(
            EndpointName=SAGEMAKER_ENDPOINT_NAME,
            ContentType="application/json",
            Body=json.dumps({"sequence": sequence}),
        )
    except Exception as e:
        raise RuntimeError(
            f"SageMaker invoke failed for endpoint '{SAGEMAKER_ENDPOINT_NAME}': {e}"
        )

    data = json.loads(response["Body"].read().decode())
    raw = data.get("embedding")
    if not raw:
        raise ValueError("No embedding in ESMC response")

    embedding = np.array(raw, dtype=np.float32)
    if embedding.ndim != 1:
        raise ValueError(f"Unexpected embedding shape: {embedding.shape}")
    # The container owns pooling now, so a silent dimension change there would
    # otherwise corrupt the cosine search against the reference index.
    if embedding.shape[0] != EMBEDDING_DIM:
        raise ValueError(
            f"Expected a {EMBEDDING_DIM}-d embedding, got {embedding.shape[0]}"
        )
    return embedding


def _search(query_embedding: np.ndarray, top_k: int) -> list[tuple[str, float]]:
    """Return the top_k (id, cosine_similarity) pairs from the reference index."""
    query_norm = query_embedding / (np.linalg.norm(query_embedding) + 1e-10)
    scores = _REF_EMBEDDINGS @ query_norm  # dot product against pre-normalised matrix
    k = min(top_k, len(scores))
    top_indices = np.argpartition(scores, -k)[-k:]
    top_indices = top_indices[np.argsort(scores[top_indices])[::-1]]
    return [(str(_REF_IDS[i]), float(scores[i])) for i in top_indices]


def lambda_handler(event, _context):
    sequence = event.get("sequence")
    if not sequence or not isinstance(sequence, str):
        raise ValueError("Event must contain a non-empty 'sequence' string")

    logger.info("Embedding screening: sequence_length=%d", len(sequence))

    embedding = _embed_sequence(sequence)
    hits = _search(embedding, TOP_K)
    max_similarity = hits[0][1] if hits else 0.0

    logger.info("Embedding screening complete: max_similarity=%.4f", max_similarity)

    return {
        "hits": [{"id": hit_id, "similarity": similarity} for hit_id, similarity in hits],
        "count": len(hits),
        "max_similarity": max_similarity,
    }
