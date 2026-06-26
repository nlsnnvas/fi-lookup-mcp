#!/usr/bin/env python3
"""Regenerate the dependency/license inventory for SBOM.md.

Reads the pinned requirements files and the installed package metadata
(importlib.metadata) — no network. Prints a markdown table per manifest plus a
license-count summary. Pipe/paste the output into SBOM.md, or run with --check
to diff package versions against what SBOM.md currently lists.

    python tools/gen_sbom.py
"""
from __future__ import annotations

import importlib.metadata as md
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MANIFESTS = [
    ("Runtime", "requirements.txt"),
    ("Test", "requirements-dev.txt"),
    ("Optional JS tier", "requirements-js.txt"),
]


def parse_reqs(path: str) -> list[str]:
    names: list[str] = []
    full = os.path.join(ROOT, path)
    if not os.path.exists(full):
        return names
    for line in open(full):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        names.append(re.split(r"[<>=!~ ]", line, 1)[0])
    return names


def license_of(dist: md.Distribution) -> str:
    meta = dist.metadata
    expr = (meta.get("License-Expression") or "").strip()
    if expr:
        return expr
    classifiers = [
        v.split("::")[-1].strip()
        for k, v in meta.items()
        if k == "Classifier"
        and v.startswith("License ::")
        and "OSI Approved" not in v.split("::")[-1]
    ]
    if classifiers:
        return classifiers[-1]
    lic = (meta.get("License") or "").strip()
    if lic and len(lic) <= 40:
        return lic
    return "(license text in metadata)" if lic else "UNKNOWN"


def main() -> int:
    for label, path in MANIFESTS:
        print(f"\n## {label} (`{path}`)\n")
        print("| Package | Version | License |")
        print("|---------|---------|---------|")
        for name in sorted(parse_reqs(path), key=str.lower):
            try:
                dist = md.distribution(name)
                print(f"| {dist.metadata['Name']} | {dist.version} | {license_of(dist)} |")
            except md.PackageNotFoundError:
                print(f"| {name} | (not installed) | |")
    return 0


if __name__ == "__main__":
    sys.exit(main())
