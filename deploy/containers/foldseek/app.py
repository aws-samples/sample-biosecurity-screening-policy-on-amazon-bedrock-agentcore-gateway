# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import json
from flask import Flask, Response, request
from inference import search

app = Flask(__name__)


@app.get("/ping")
def ping():
    return Response("OK", status=200, mimetype="text/plain")


@app.post("/invocations")
def invocations():
    body = request.get_json(force=True)
    sequence = body.get("sequence")
    if not sequence or not isinstance(sequence, str):
        return Response(
            json.dumps({"error": "body must contain a non-empty 'sequence' string"}),
            status=400,
            mimetype="application/json",
        )
    hits = search(sequence)
    min_evalue = min((h["evalue"] for h in hits), default=None)
    return Response(
        json.dumps({"hits": hits, "count": len(hits), "min_evalue": min_evalue}),
        status=200,
        mimetype="application/json",
    )
