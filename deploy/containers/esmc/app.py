# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import json

from flask import Flask, Response, request

from inference import MAX_SEQUENCE_LENGTH, embed, ready

app = Flask(__name__)


def _json_error(message: str, status: int) -> Response:
    return Response(json.dumps({"error": message}), status=status, mimetype="application/json")


@app.get("/ping")
def ping():
    # A meaningful health check: SageMaker keeps routing traffic to any container
    # that returns 200, so reporting healthy before the model loads would produce
    # sustained invocation errors. Must stay cheap — the /ping timeout is 2s.
    if not ready():
        return Response("model not loaded", status=503, mimetype="text/plain")
    return Response("OK", status=200, mimetype="text/plain")


@app.post("/invocations")
def invocations():
    body = request.get_json(force=True)
    sequence = body.get("sequence")
    if not sequence or not isinstance(sequence, str):
        return _json_error("body must contain a non-empty 'sequence' string", 400)
    if len(sequence) > MAX_SEQUENCE_LENGTH:
        return _json_error(
            f"sequence length {len(sequence)} exceeds the maximum of {MAX_SEQUENCE_LENGTH}", 400
        )
    embedding = embed(sequence)
    return Response(
        json.dumps({"embedding": embedding}),
        status=200,
        mimetype="application/json",
    )
