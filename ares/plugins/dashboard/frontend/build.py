#!/usr/bin/env python3
"""Build the dashboard's served page from source.

The dashboard is a single self-contained Preact page (no npm/build toolchain).
This step inlines the vendored Preact bundle into the source template so the
server can ship one file with no external requests.

  source:   frontend/index.html                     (has the PREACT marker below)
  vendor:   frontend/htm-preact-standalone.umd.js    (pinned htm@3.1.1 preact standalone)
  output:   static/index.html                        (self-contained, served by api.py)

Run from anywhere:
  python ares/plugins/dashboard/frontend/build.py
"""
from __future__ import annotations

from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE / "index.html"
LIB = HERE / "htm-preact-standalone.umd.js"
OUT = HERE.parent / "static" / "index.html"
MARKER = "/*__PREACT_STANDALONE__*/"


def main() -> None:
    html = SRC.read_text(encoding="utf-8")
    lib = LIB.read_text(encoding="utf-8")
    if html.count(MARKER) != 1:
        raise SystemExit(f"expected exactly one {MARKER} in {SRC}, found {html.count(MARKER)}")
    OUT.write_text(html.replace(MARKER, lib), encoding="utf-8")
    print(f"built {OUT} ({OUT.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
