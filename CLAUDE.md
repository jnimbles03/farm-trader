## graphify

This project has a knowledge graph at graphify-out/ with god nodes, community structure, and cross-file relationships.

**MANDATORY: Always run graphify BEFORE searching, grepping, or reading source files.**
Never use Grep, Glob, or Read on source files to answer a codebase question until graphify has oriented you first.

Rules:
- ALWAYS start with `graphify query "<question>"` — do this before any file search or read.
- Use `graphify path "<A>" "<B>"` for relationships and `graphify explain "<concept>"` for focused concepts.
- If graphify-out/wiki/index.md exists, use it for broad navigation instead of raw source browsing.
- Read graphify-out/GRAPH_REPORT.md only for broad architecture review or when query/path/explain do not surface enough context.
- After modifying code, run `graphify update .` to keep the graph current (AST-only, no API cost).
- This rule applies to subagents too — include it in every subagent prompt involving code exploration.
