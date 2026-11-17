#!/usr/bin/env python3
"""Regenerate include/api/manju_pipeline.h from tools/manju/manju_pipeline.py.

Usage: python3 tools/gen_manju_pipeline.py
Keeps the embedded 漫剧 production pipeline in sync with the canonical script
so POST /v1/manju/import can materialise it inside any workspace.
"""
import os
from tempfile import NamedTemporaryFile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "tools", "manju", "manju_pipeline.py")
DST = os.path.join(ROOT, "include", "api", "manju_pipeline.h")

with open(SRC, "r", encoding="utf-8") as f:
    text = f.read()

lines = []
lines.append("/* manju_pipeline.h - GENERATED from tools/manju/manju_pipeline.py")
lines.append(" * by tools/gen_manju_pipeline.py. Do not edit by hand; re-run the")
lines.append(" * generator after changing the pipeline script. */")
lines.append("#pragma once")
lines.append("static const char manju_pipeline_py[] =")
for line in text.split("\n"):
    esc = line.replace("\\", "\\\\").replace('"', '\\"')
    lines.append('    "%s\\n"' % esc)
lines.append("    ;")

content = ("\n".join(lines) + "\n").encode("utf-8")
try:
    with open(DST, "rb") as f:
        unchanged = f.read() == content
except FileNotFoundError:
    unchanged = False

if not unchanged:
    pending = None
    try:
        with NamedTemporaryFile(dir=os.path.dirname(DST), delete=False) as f:
            pending = f.name
            f.write(content)
        os.replace(pending, DST)
        pending = None
    finally:
        if pending is not None:
            os.unlink(pending)

print("wrote %s (%d bytes)" % (DST, os.path.getsize(DST)))