"""Pulls just the raw analyze_calls tree text out of probe_output.txt for parser testing."""

with open("scripts/probe_output.txt", encoding="utf-8") as f:
    text = f.read()

marker = "=== analyze_calls (depth=3) raw text ===\n"
idx = text.index(marker) + len(marker)
tree_text = text[idx:]

with open("scripts/probe_tree_only.txt", "w", encoding="utf-8") as f:
    f.write(tree_text)

print(f"wrote {len(tree_text)} chars")
