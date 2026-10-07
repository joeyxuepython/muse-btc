"""Verify a cloud replay against the published local reference, without trading."""

import argparse
import json
import math
from pathlib import Path

from run import HERE, digest, load_protocol


def compare(expected, actual, path="root"):
    if isinstance(expected, dict):
        if not isinstance(actual, dict) or set(expected) != set(actual):
            raise ValueError(f"Different keys at {path}")
        for key in expected:
            compare(expected[key], actual[key], f"{path}.{key}")
    elif isinstance(expected, list):
        if not isinstance(actual, list) or len(expected) != len(actual):
            raise ValueError(f"Different list length at {path}")
        for i, (a, b) in enumerate(zip(expected, actual, strict=True)):
            compare(a, b, f"{path}[{i}]")
    elif isinstance(expected, float):
        if (
            isinstance(actual, bool)
            or not isinstance(actual, (int, float))
            or not math.isclose(expected, actual, rel_tol=1e-9, abs_tol=1e-10)
        ):
            raise ValueError(f"Numeric mismatch at {path}: {expected} != {actual}")
    elif type(expected) is not type(actual) or expected != actual:
        raise ValueError(f"Mismatch at {path}: {expected!r} != {actual!r}")


def verify(output):
    load_protocol()
    if digest(output / "protocol.json") != digest(HERE / "protocol.json"):
        raise ValueError("Output protocol differs from frozen study")
    manifest = json.loads((output / "SHA256SUMS.json").read_text())
    for name, expected in manifest.items():
        path = (output / name).resolve()
        if not path.is_relative_to(output.resolve()) or digest(path) != expected:
            raise ValueError(f"Invalid output checksum: {name}")
    actual_files = {
        str(p.relative_to(output))
        for p in output.rglob("*")
        if p.is_file() and p.name != "SHA256SUMS.json"
    }
    if actual_files != set(manifest):
        raise ValueError("Output manifest does not cover exactly all result files")
    provenance = json.loads((output / "provenance.json").read_text())
    for name, sha in provenance["code_sha256"].items():
        if name not in ("baseline.py", "run.py", "uv.lock") or digest(HERE / name) != sha:
            raise ValueError(f"Different code used for replay: {name}")
    if set(provenance["code_sha256"]) != {"baseline.py", "run.py", "uv.lock"}:
        raise ValueError("Missing code provenance")
    compare(
        json.loads((HERE / "expected-results.json").read_text()),
        json.loads((output / "results.json").read_text()),
    )
    return {
        "reproducibility_passed": True,
        "production_authorized": False,
        "warning": "Replay agreement is not evidence of profitability or live readiness",
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    print(json.dumps(verify(parser.parse_args().output), indent=2))
