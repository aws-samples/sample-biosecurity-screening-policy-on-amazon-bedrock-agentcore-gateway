# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Downloads PDB structure files from RCSB into the Foldseek Docker build context directory.

Usage:
    python scripts/prepare_foldseek_structures.py [--fasta PATH] [--output-dir PATH] [--workers N]
"""

import argparse
import http.client
import re
import ssl
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

RCSB_PDB_HOST = "files.rcsb.org"
PDB_ID_PATTERN = re.compile(r"[A-Za-z0-9]{4}")
_SSL_CONTEXT = ssl.create_default_context()

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FASTA = PROJECT_ROOT / "deploy" / "containers" / "mmseqs2" / "SafeProtein_Bench.fasta"
DEFAULT_OUTPUT = PROJECT_ROOT / "deploy" / "containers" / "foldseek" / "structures"


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


def download_pdb(pdb_id: str, output_dir: Path) -> tuple[str, bool, str]:
    dest = output_dir / f"{pdb_id.lower()}.pdb"
    if dest.exists():
        return pdb_id, True, "already exists"
    try:
        status, reason, data = _download_from_rcsb(pdb_id)
        if status != 200:
            return pdb_id, False, f"HTTP {status}: {reason}"
        dest.write_bytes(data)
        return pdb_id, True, ""
    except (OSError, http.client.HTTPException) as exc:
        return pdb_id, False, f"HTTPS error: {exc}"
    except Exception as exc:
        return pdb_id, False, str(exc)


def main() -> None:
    parser = argparse.ArgumentParser(description="Download PDB files from RCSB for Foldseek DB build")
    parser.add_argument("--fasta", type=Path, default=DEFAULT_FASTA,
                        help=f"FASTA file to extract PDB IDs from (default: {DEFAULT_FASTA})")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT,
                        help=f"Local output directory (default: {DEFAULT_OUTPUT})")
    parser.add_argument("--workers", type=int, default=10, help="Parallel download workers (default: 10)")
    args = parser.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)

    pdb_ids = extract_pdb_ids(args.fasta)
    print(f"Found {len(pdb_ids)} unique PDB IDs in {args.fasta}")

    successes, failures = [], []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(download_pdb, pdb_id, args.output_dir): pdb_id
            for pdb_id in pdb_ids
        }
        for i, future in enumerate(as_completed(futures), 1):
            pdb_id, ok, msg = future.result()
            if ok:
                successes.append(pdb_id)
                print(f"[{i}/{len(pdb_ids)}] OK   {pdb_id}" + (f" ({msg})" if msg else ""))
            else:
                failures.append((pdb_id, msg))
                print(f"[{i}/{len(pdb_ids)}] FAIL {pdb_id}: {msg}", file=sys.stderr)
            time.sleep(0.05)  # nosemgrep -- rate-limit backoff against RCSB in this offline, one-off data-prep script; not runtime/deployed code.

    print(f"\nDone: {len(successes)} downloaded, {len(failures)} failed.")
    if failures:
        print("Failed IDs:")
        for pdb_id, msg in sorted(failures):
            print(f"  {pdb_id}: {msg}")
        sys.exit(1)


if __name__ == "__main__":
    main()
