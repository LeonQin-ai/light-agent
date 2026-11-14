#!/usr/bin/env python3
"""Regenerate include/api/web_ui.h from apps/web/index.html.

Usage: python3 tools/gen_web_ui.py
Keeps the embedded web UI string in sync with the canonical HTML file.
"""
import os
from tempfile import NamedTemporaryFile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "apps", "web", "index.html")
DST = os.path.join(ROOT, "include", "api", "web_ui.h")

with open(SRC, "r", encoding="utf-8") as f:
    text = f.read()

lines = []
lines.append("/* web_ui.h - GENERATED from apps/web/index.html by tools/gen_web_ui.py.")
lines.append(" * Do not edit by hand; re-run the generator after changing the HTML. */")
lines.append("#pragma once")
lines.append("static const char web_index_html[] =")
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
