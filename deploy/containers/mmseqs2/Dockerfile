# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

FROM public.ecr.aws/lambda/python:3.12

RUN dnf install -y gawk tar gzip wget shadow-utils && dnf clean all

RUN curl -fsSL -o /tmp/mmseqs.tar.gz \
        https://github.com/soedinglab/MMseqs2/releases/latest/download/mmseqs-linux-avx2.tar.gz && \
    tar -xzf /tmp/mmseqs.tar.gz -C /tmp && \
    mv /tmp/mmseqs/bin/mmseqs /usr/local/bin/mmseqs && \
    rm -rf /tmp/mmseqs.tar.gz /tmp/mmseqs

COPY handler.py ${LAMBDA_TASK_ROOT}/

COPY SafeProtein_Bench.fasta /home/data/SafeProtein_Bench.fasta

RUN mmseqs createdb /home/data/SafeProtein_Bench.fasta /home/data/targetDB && \
    /usr/sbin/useradd --system --create-home appuser && \
    chown -R appuser:appuser ${LAMBDA_TASK_ROOT} /home/data

USER appuser

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD test -x /usr/local/bin/mmseqs || exit 1
CMD ["handler.lambda_handler"]
