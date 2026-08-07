# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

FROM public.ecr.aws/docker/library/python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        libgomp1 && \
    rm -rf /var/lib/apt/lists/*

RUN curl -fsSL -o /tmp/foldseek.tar.gz \
        https://mmseqs.com/foldseek/foldseek-linux-avx2.tar.gz && \
    tar -xzf /tmp/foldseek.tar.gz -C /tmp && \
    mv /tmp/foldseek/bin/foldseek /usr/local/bin/foldseek && \
    rm -rf /tmp/foldseek.tar.gz /tmp/foldseek

# Build target database from PDB structures
COPY structures/ /opt/ml/data/structures/
RUN foldseek createdb /opt/ml/data/structures/ /opt/ml/data/targetDB && \
    foldseek createindex /opt/ml/data/targetDB /tmp && \
    rm -rf /tmp/* /opt/ml/data/structures/

# Download ProstT5 weights
RUN foldseek databases ProstT5 /opt/ml/data/prostt5_weights /tmp && \
    rm -rf /tmp/*

RUN pip install --no-cache-dir flask gunicorn

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
