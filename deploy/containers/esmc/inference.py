# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""ESM Cambrian 600M inference for the custom SageMaker BYOC endpoint.

Loads the open-source ESMC-600M weights once at import time and exposes a
single ``embed`` function that turns a protein sequence into a mean-pooled
1152-dimensional embedding vector.

The mean-pooling and BOS/EOS stripping that previously lived in the
embedding-screening Lambda now happen here, so callers send ``{"sequence": ...}``
and receive ``{"embedding": [...1152 floats...]}`` in a single request.

The weights are baked into the image at ``/opt/esmc-600m`` during the Docker
build, so ``from_pretrained`` resolves entirely from disk with no network
access — which is what lets the endpoint run under SageMaker network isolation.
"""

import logging
import threading

import torch
from transformers import AutoModel, AutoTokenizer

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

# Directory and immutable revision of the weights snapshot_download'ed into the image.
MODEL_DIR = "/opt/esmc-600m"
MODEL_REVISION = "a7e82012c83126b9eedb055fea9fa84b6c02f094"

# ESM Cambrian 600M has a model dimension of 1152 (config.json: d_model).
EMBEDDING_DIM = 1152

# Attention is O(L^2) on the pure-PyTorch SDPA path, and the sequence arrives
# from an untrusted tool argument with no upper bound on length, so cap it.
# The SafeProtein_Bench reference set maxes out at 962 residues.
MAX_SEQUENCE_LENGTH = 2048

_DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
logger.info("Loading ESMC-600M from %s onto %s", MODEL_DIR, _DEVICE)

_tokenizer = AutoTokenizer.from_pretrained(
    MODEL_DIR, revision=MODEL_REVISION, local_files_only=True
)
# AutoModel resolves to ESMCModel (the bare encoder) via config.model_type
# "esmc". Deliberately not ESMCForMaskedLM — the LM head is never used here.
_model = AutoModel.from_pretrained(
    MODEL_DIR, revision=MODEL_REVISION, local_files_only=True
).to(_DEVICE).eval()

# The endpoint is served by a single gthread worker, so requests can arrive
# concurrently. Serialise the forward pass so activations don't stack on the GPU.
_lock = threading.Lock()

logger.info("Model loaded")


def ready() -> bool:
    """Whether the model finished loading and can serve inference."""
    return _model is not None


def embed(sequence: str) -> list[float]:
    """Return a mean-pooled float32 embedding (length 1152) for a protein sequence."""
    if len(sequence) > MAX_SEQUENCE_LENGTH:
        raise ValueError(
            f"Sequence length {len(sequence)} exceeds the maximum of {MAX_SEQUENCE_LENGTH}"
        )

    # The tokenizer wraps the sequence as "<cls> ... <eos>", so input length is L+2.
    inputs = _tokenizer([sequence.upper()], return_tensors="pt")
    if inputs["input_ids"].shape[0] != 1:
        raise ValueError("embed() handles exactly one sequence per call")
    inputs = {k: v.to(_DEVICE) for k, v in inputs.items()}

    with _lock, torch.inference_mode():
        # bf16 autocast on GPU matches the precision the upstream ESM SDK used.
        with torch.autocast("cuda", dtype=torch.bfloat16, enabled=(_DEVICE == "cuda")):
            output = _model(**inputs)

        embeddings = output.last_hidden_state
        if embeddings is None:
            raise ValueError("Model returned no embeddings")
        # Widen before pooling: under autocast last_hidden_state is bf16, and
        # averaging first would quantise the result to ~3 significant digits.
        embeddings = embeddings.float()

    if embeddings.ndim != 3:
        raise ValueError(f"Unexpected embeddings shape: {tuple(embeddings.shape)}")

    # Strip <cls>/<eos> and mean-pool across residues.
    residues = embeddings[0][1:-1]
    if residues.shape[0] == 0:
        raise ValueError("No residue embeddings after stripping BOS/EOS")

    return residues.mean(dim=0).cpu().tolist()
