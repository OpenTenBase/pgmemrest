#!/usr/bin/env python3
# Copyright (c) 2026 gechengfu. SPDX-License-Identifier: MIT
"""Verify pinned Apache AGE build sources and apply the OpenTenBase patch.

Copies only build inputs, LICENSE and NOTICE; leaves the source tree untouched.
Use --source with an upstream checkout/archive, or --download for public files.
"""
import argparse
import concurrent.futures
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--source", type=Path)
    inputs.add_argument("--download", action="store_true")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    manifest = json.loads((ROOT / "scripts/upstream-files.json").read_text())
    output = args.output.resolve()
    if output.exists():
        parser.error("output already exists; choose a new directory")
    output.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".age-prepare-", dir=output.parent))
    try:
        def fetch(entry):
            name = entry["path"]
            relative = Path(name)
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError("unsafe manifest path")
            if args.source:
                data = (args.source / relative).read_bytes()
            else:
                url = "https://raw.githubusercontent.com/apache/age/" + manifest["commit"] + "/" + name
                for attempt in range(3):
                    try:
                        with urllib.request.urlopen(url, timeout=30) as response:
                            data = response.read()
                        break
                    except OSError:
                        if attempt == 2:
                            raise
                        time.sleep(attempt + 1)
            digest = hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()
            if digest != entry["git_blob_sha1"]:
                raise ValueError("upstream content mismatch: " + name)
            destination = stage / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(fetch, manifest["files"]))
        patch = ROOT / "patches/apache-age-pg11-opentenbase.patch"
        subprocess.run(["git", "apply", "--check", str(patch)], cwd=stage, check=True)
        subprocess.run(["git", "apply", str(patch)], cwd=stage, check=True)
        (stage / "UPSTREAM_COMMIT").write_text(manifest["commit"] + "\n")
        stage.rename(output)
        print(f"Verified {len(manifest['files'])} upstream build files at {manifest['commit']}")
        print(f"Applied OpenTenBase adaptation: {output}")
    finally:
        if stage.exists():
            shutil.rmtree(stage)


if __name__ == "__main__":
    main()
