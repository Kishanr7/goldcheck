# Changelog

## 0.6.0 (2026-11-19)
- `check-sql` / MCP `check_sql`: catches text values a query compares with that are not in the data (with the closest real values), filters that remove nothing, and empty results. No gold answer needed. On BIRD: flags 50% of ChatGPT's silently wrong answers with 0.9% false alarms; its top suggestion alone fixes 77.
- `describe` / MCP `describe_values`: tables, columns, or a column's real values with counts.
- Works on SQLite, DuckDB/Parquet/CSV/JSON and Postgres, read-only like everything else.

## 0.5.0 (2026-11-17): portfolio release
- `pip install` gives a `goldcheck` command; the demo writes a ready-made query pair and prints the `compare` command.
- Reports: no sideways scrolling on 320–375 px phones; "0 rows" stated plainly; checked in 11 browser setups in light and dark mode.
- CI on Windows, macOS and Linux (Python 3.10 and 3.13) plus Postgres 17. Fixed a Python 3.10 bug in the SQLite authorizer reset.
- Docs: write-up, architecture, security and contributing notes.

## 0.4.0 (2026-11-17)
- Postgres engine, read-only by construction; shrinking on session TEMP copies.

## 0.3.0 (2026-11-17)
- Cascading deletes: self-referencing tables shrink (BIRD: 100/100 on a fresh sample).
- DuckDB engine for `.duckdb`, Parquet, CSV and JSON.

## 0.2.0 (2026-11-16)
- `compare` (old vs new query), clause blame, `--export-tests`, MCP server.
- Shrinking works on large real databases: query-plan table detection, literal and sample guesses, informative counterexamples.

## 0.1.0 (2026-11-14)
- Score a chatbot's SQL against gold answers; HTML report; demo.
