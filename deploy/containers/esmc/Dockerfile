# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

# ESMC-600M BYOC inference container for SageMaker real-time hosting.
#
# The torch CUDA wheels below bundle their own CUDA runtime, so no CUDA base
# image is needed — only the host GPU driver, which SageMaker ml.g5 instances
# provide. libgomp1 is torch's one system dependency.
FROM public.ecr.aws/docker/library/python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        libgomp1 && \
    rm -rf /var/lib/apt/lists/*

# Install the CUDA 12.4 torch wheel from the PyTorch index. Kept as its own
# layer because that index does not carry tokenizers/safetensors, so merging it
# with the install below would fail dependency resolution.
RUN pip install --no-cache-dir \
        torch==2.4.1 --index-url https://download.pytorch.org/whl/cu124

# Fail the build if anything downgraded us to the CPU-only wheel from PyPI.
RUN python3 -c "import torch; assert torch.version.cuda == '12.4', torch.version.cuda"

# ESMC is not in upstream transformers — the config declares model_type "esmc"
# but ships no modeling code and no auto_map, so trust_remote_code cannot help.
# The loader lives in Biohub's transformers fork, pinned here by commit so the
# build is reproducible. (CDK's DockerImageAsset hashes the build context, not
# resolved dependencies, so an unpinned @main could change the image without
# changing the asset hash.) Installed from the codeload tarball rather than
# git+https because python:3.12-slim has no git.
#
# Bump deliberately: security updates to the fork require editing this SHA.
RUN pip install --no-cache-dir \
        "transformers @ https://github.com/Biohub/transformers/archive/ef32577f55da19a4989cd7b22e004dc43a4998cb.tar.gz" \
        accelerate \
        flask \
        gunicorn

# Bake the open-source ESMC-600M weights into the image so there is no
# HuggingFace download at cold start, which is what lets the endpoint run under
# SageMaker network isolation. Pinned to a revision for reproducibility.
RUN python3 -c "\
from huggingface_hub import snapshot_download; \
snapshot_download('biohub/ESMC-600M', \
                  revision='a7e82012c83126b9eedb055fea9fa84b6c02f094', \
                  local_dir='/opt/esmc-600m')"

# Belt-and-braces: with these set, a stray hub call fails fast instead of
# burning the full TCP retry budget against an isolated network.
ENV HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1

# Verify the weights actually load into ESMCModel. The checkpoint was saved from
# ESMCForMaskedLM, so its keys are "esmc.*" and loading into the bare encoder
# relies on prefix stripping. If that ever breaks, transformers only *warns* and
# hands back a randomly initialised model that still returns a finite 1152-d
# vector — so assert on the load report, not just on the output shape.
# lm_head.* legitimately appears in unexpected_keys and is ignored.
RUN python3 -c "\
from transformers import AutoModel, AutoTokenizer; \
model, info = AutoModel.from_pretrained('/opt/esmc-600m', local_files_only=True, output_loading_info=True); \
assert not info['missing_keys'], info['missing_keys']; \
assert not info['mismatched_keys'], info['mismatched_keys']; \
tok = AutoTokenizer.from_pretrained('/opt/esmc-600m', local_files_only=True); \
out = model(**tok(['MLKNVQVQLV'], return_tensors='pt')); \
assert out.last_hidden_state.shape == (1, 12, 1152), out.last_hidden_state.shape; \
print('build smoke test OK')"

COPY inference.py /opt/ml/code/inference.py
COPY app.py /opt/ml/code/app.py
COPY serve /opt/ml/code/serve
RUN chmod +x /opt/ml/code/serve && \
    useradd --system --create-home appuser && \
    chown -R appuser:appuser /opt/ml/code

USER appuser

ENV PATH="/opt/ml/code:${PATH}"
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s --start-period=120s --retries=3 \
    CMD curl --fail --silent --show-error http://localhost:8080/ping || exit 1
ENTRYPOINT ["serve"]
