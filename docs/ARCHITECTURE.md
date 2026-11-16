# Architecture

goldcheck is a small Python package with no required dependencies. Each module has one job.

| Module | Job |
|---|---|
| `core.py` | Open a database read-only, run one query under a timeout, compare two results (column order and names ignored, numbers to 6 significant digits, rows as a bag unless order matters), score a set of questions |
| `shrink.py` | Find the smallest set of rows on which two queries still disagree (SQLite), and the `Counterexample` type shared by all engines |
| `blame.py` | Split both queries into top-level clauses; swap one or two clauses for the trusted version, re-run on the counterexample, confirm on the full database |
| `lock.py` | Export counterexamples as standalone regression tests (standard library only) |
| `report.py` | One self-contained HTML file: no scripts, no external requests, light and dark themes |
| `duck.py` | DuckDB engine: `.duckdb` files, Parquet/CSV/JSON files and folders |
| `pg.py` | Postgres engine, read-only by construction |
| `mcp.py` | MCP server over stdio with one tool, `compare_queries` |
| `demo.py` | The fictional shop database, 12 questions and a careless agent |
| `__main__.py` | The `goldcheck` command: `demo`, `run`, `compare`, `shrink`, `mcp` |

## Data flow

```
trusted SQL ─┐                                   ┌─ verdict (same / wrong values / missing rows / …)
             ├─ core.score / compare ────────────┤
checked SQL ─┘          │ silently wrong          └─ report.html, exit code
                        ▼
              shrink: copy → delete rows → re-check ─→ Counterexample (real rows, 1-minimal)
                        │                                   │
                        ▼                                   ▼
              blame: swap clauses, re-run          lock: fixture.sql + expected.json + test
```

## The shrinking loop

1. Find the tables the two queries read: SQLite and Postgres from the query plan (`EXPLAIN`), DuckDB from its parser. Other tables are never touched.
2. Work on a private copy: a file copy for SQLite, an in-memory copy for DuckDB, session TEMP tables for Postgres.
3. Check that the queries still disagree silently on the copy. If the trusted result has real data, the counterexample must keep some ("informative").
4. Guesses, each verified like any other step: rows matching a text literal in either query (plus foreign-key neighbours); then random samples of 1,000, 100 and 10 rows of each large table.
5. Delta debugging per table: try deleting the whole table, then halves, quarters and so on down to single rows. A deletion also removes the rows that point at the deleted ones (declared foreign keys), so references never dangle. Each attempt runs inside a savepoint or transaction and is rolled back if the disagreement disappears.
6. Stop when a full pass removes nothing ("1-minimal": no single row and its dependants can go) or the budget (60 s, 3,000 checks) runs out. The report says which.

## Safety model

Checked SQL is untrusted input:

- **SQLite:** read-only connection; an authorizer allows only read actions; a progress handler enforces the timeout.
- **DuckDB:** in-memory copy; `enable_external_access = false` and `lock_configuration = true` block files, network, `ATTACH`, `COPY` and extensions; only a single SELECT statement; `interrupt()` on timeout.
- **Postgres:** must start with SELECT/WITH/VALUES/TABLE; sent with the extended protocol (one statement); runs in a READ ONLY transaction inside a savepoint that is always rolled back; `statement_timeout`. Shrinking uses session TEMP tables and refuses schema-qualified queries.

Reports and exports contain real rows from the user's database and never the Postgres password.
