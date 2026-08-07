# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Downloads PDB structure files referenced in a FASTA file and uploads them to S3.

The PDB ID is the portion of the FASTA header after the last underscore
(e.g. ">A0A096YGU7_6J5F" → PDB ID "6J5F").

Usage:
    python scripts/download_pdb.py [--fasta PATH] [--bucket NAME] [--prefix S3_PREFIX]
                                   [--workers N] [--dry-run]
"""

import argparse
import http.client
import re
import ssl
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import boto3

RCSB_PDB_HOST = "files.rcsb.org"
PDB_ID_PATTERN = re.compile(r"[A-Za-z0-9]{4}")
_SSL_CONTEXT = ssl.create_default_context()

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FASTA = PROJECT_ROOT / "deploy" / "containers" / "mmseqs2" / "SafeProtein_Bench.fasta"
DEFAULT_BUCKET = None
DEFAULT_PREFIX = "pdb/"


def extract_pdb_ids(fasta_path: Path) -> list[str]:
    ids = set()
    with open(fasta_path) as f:
        for line in f:
            if line.startswith(">"):
                header = line.strip().lstrip(">")
                pdb_id = header.rsplit("_", 1)[-1]
                if pdb_id:
                    ids.add(pdb_id.upper())
    return sorted(ids)


def _download_from_rcsb(pdb_id: str) -> tuple[int, str, bytes]:
    if not PDB_ID_PATTERN.fullmatch(pdb_id):
        raise ValueError(f"Invalid PDB ID: {pdb_id!r}")

    # nosemgrep: python.lang.security.audit.httpsconnection-detected.httpsconnection-detected -- Fixed HTTPS host with a certificate- and hostname-verifying default SSL context.
    connection = http.client.HTTPSConnection(RCSB_PDB_HOST, timeout=30, context=_SSL_CONTEXT)
    try:
        connection.request("GET", f"/download/{pdb_id.lower()}.pdb")
        response = connection.getresponse()
        return response.status, response.reason, response.read()
    finally:
        connection.close()


def download_and_upload(pdb_id: str, bucket: str, prefix: str, s3_client) -> tuple[str, bool, str]:
    s3_key = f"{prefix}{pdb_id.lower()}.pdb"
    try:
        status, reason, data = _download_from_rcsb(pdb_id)
        if status != 200:
            return pdb_id, False, f"HTTP {status}: {reason}"
        s3_client.put_object(Bucket=bucket, Key=s3_key, Body=data, ContentType="chemical/x-pdb")
        return pdb_id, True, ""
    except (OSError, http.client.HTTPException) as exc:
        return pdb_id, False, f"HTTPS error: {exc}"
    except Exception as exc:
        return pdb_id, False, str(exc)


def main() -> None:
    parser = argparse.ArgumentParser(description="Download PDB files and upload to S3")
    parser.add_argument("--fasta", type=Path, default=DEFAULT_FASTA,
                        help=f"Path to FASTA file (default: {DEFAULT_FASTA})")
    parser.add_argument("--bucket", required=True,
                        help="S3 bucket name for uploading PDB files")
    parser.add_argument("--prefix", default=DEFAULT_PREFIX,
                        help=f"S3 key prefix (default: {DEFAULT_PREFIX})")
    parser.add_argument("--workers", type=int, default=10,
                        help="Number of parallel download workers (default: 10)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print PDB IDs that would be downloaded without fetching anything")
    args = parser.parse_args()

    pdb_ids = extract_pdb_ids(args.fasta)
    print(f"Found {len(pdb_ids)} unique PDB IDs in {args.fasta}")

    if args.dry_run:
        for pdb_id in pdb_ids:
            s3_key = f"{args.prefix}{pdb_id.lower()}.pdb"
            print(f"  {pdb_id}  →  s3://{args.bucket}/{s3_key}")
        return

    s3 = boto3.client("s3")
    successes, failures = [], []

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(download_and_upload, pdb_id, args.bucket, args.prefix, s3): pdb_id
            for pdb_id in pdb_ids
        }
        for i, future in enumerate(as_completed(futures), 1):
            pdb_id, ok, err = future.result()
            if ok:
                successes.append(pdb_id)
                print(f"[{i}/{len(pdb_ids)}] OK   {pdb_id}")
            else:
                failures.append((pdb_id, err))
                print(f"[{i}/{len(pdb_ids)}] FAIL {pdb_id}: {err}", file=sys.stderr)
            # brief pause to avoid hammering RCSB
            time.sleep(0.05)  # nosemgrep -- rate-limit backoff against RCSB in this offline, one-off data-prep script; not runtime/deployed code.

    print(f"\nDone: {len(successes)} succeeded, {len(failures)} failed.")
    if failures:
        print("Failed IDs:")
        for pdb_id, err in sorted(failures):
            print(f"  {pdb_id}: {err}")
        sys.exit(1)


if __name__ == "__main__":
    main()
