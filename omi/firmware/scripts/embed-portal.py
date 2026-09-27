"""Embed the local setup page without runtime filesystem dependencies."""
import json
import pathlib
import sys
source, output = map(pathlib.Path, sys.argv[1:])
output.write_text("/* Generated from portal.html. */\nstatic const char portal_page[] =\n" +
                  "".join(json.dumps(line) + "\n" for line in source.read_text().splitlines(keepends=True)) + ";\n")
