# goldcheck

**A debugger for SQL that returns the wrong result: find it, see the smallest real data that proves it, pin the clause, and lock it in a test.**

> Status: `PORTFOLIO-ONLY` · `UNVALIDATED`. An engineering portfolio project. No claim is made that anyone needs it, uses it, or prefers it to alternatives. Tested by its author's AI agent on Windows 11 and Linux, on fictional data and on the public BIRD benchmark (see `research/FINAL_REPORT_2026-11-16.md`).

SQL that runs cleanly can still answer the wrong question: a chatbot that forgets a status filter, a refactor that drops a `DISTINCT`, a migration that joins on the wrong key. Nothing crashes, so nobody notices. goldcheck runs the query you trust next to the one you are checking, on your own data (SQLite, DuckDB, Parquet, CSV or JSON), and when they disagree it tells you:

1. **that** they disagree, and how (wrong values, missing rows, wrong order);
2. **on which rows**: the database is cut down to the smallest set of *real* rows that still shows the difference, often 1–3;
3. **in which clause**: one clause at a time is swapped for the trusted version and re-run, so "the WHERE clause" means it was proven by execution;
4. **how to keep it fixed**: each case is exported as a standalone regression test.

Python standard library only for SQLite. DuckDB, Parquet, CSV and JSON need `pip install duckdb`. No model, no API key, no network.

## Commands

| Command | Use it when | Needs |
| --- | --- | --- |
| `check` (`run`) | Scoring a chat-with-your-data agent against known-good answers | questions with gold SQL |
| `compare` | You rewrote, refactored, optimised or migrated a query (or an AI did) and want to know whether the result changed | the old query and the new one, **no gold answers** |
| `shrink` | You want one counterexample database as a file | as `check` |
| `--export-tests` | You want each difference as a test that fails until it is fixed | either of the above |
| `mcp` | A coding agent should check its own SQL and get the counterexample back | an MCP client |

## Try it in one minute

Needs Python 3.10+.

```
python -m goldcheck demo
```

It builds a fictional shop database, scores a deliberately careless agent and writes `goldcheck-demo/report.html`:

```
Shrunk 4 silent difference(s) to 1-4 row(s) each; see the report.
7/12 correct (58%, 95% interval 32-81%)
4 ran cleanly but returned the wrong result; 1 failed visibly.
```

In the report each silent failure shows the expected and actual results, then **the smallest database that shows the difference** (e.g. 3 rows instead of 968: one customer, one order, one item, on which the agent's extra join to `refunds` returns nothing) and **where the difference comes from** (the FROM clause: the agent joins `refunds`).

## Compare an old and a new query

```
python -m goldcheck compare --db shop.sqlite --old old.sql --new new.sql
```

`--old` and `--new` take a `.sql` file or the SQL itself. For many pairs use `--pairs pairs.jsonl` with one `{"id", "old", "new", "note"?, "ordered"?}` per line. Output:

```
Shrunk 1 silent difference(s) to 3 row(s) each; see the report.
0/1 pair(s) return the same result; 1 differ (1 silently, 0 with an error in the new query).
  pair: no column of the result holds the expected values of column 2. Smallest database: 3 row(s). Comes from the WHERE clause.
```

Exit code 0 means every pair returned the same result on this database, 1 means at least one differs, and 2 means bad input. That lets you use it as a CI gate on query refactors. Same result on one database is evidence, not proof of equivalence.

## Score a chatbot

1. Questions with gold SQL (`questions.json`): `[{"id": "q1", "question": "How many customers?", "gold_sql": "SELECT COUNT(*) FROM customers"}]`. Add `"ordered": true` when row order matters.
2. The agent's SQL as JSON Lines (`{"id": "q1", "sql": "..."}` per line), or a command goldcheck calls per question:
   ```
   python -m goldcheck run --db shop.sqlite --questions questions.json --answers answers.jsonl --shrink
   python -m goldcheck run --db shop.sqlite --questions questions.json --agent-cmd "my-agent --sql {question}" --shrink
   ```
3. `--min-accuracy 0.8` fails a CI job below a threshold. `--json` prints machine output.

## Lock it in a test

Add `--export-tests regressions/` to `run --shrink` or `compare`. Each difference becomes a folder with `fixture.sql` (the tiny database), `query.sql`, `reference.sql` and `expected.json`, plus one `test_regressions.py` that needs only Python:

```
python -m unittest discover -s regressions
```

Each case fails while the bug is there. Put the fixed SQL in `query.sql` and it passes. The fixtures contain real rows from your database, so review them before sharing.

## DuckDB, Parquet, CSV and JSON

Point `--db` at a `.duckdb` file, a `.parquet`, `.csv` or `.json` file, or a folder of them (each file becomes a table named after it). Everything works the same: `run`, `compare`, shrinking, clause blame, `--export-tests` and MCP.

```
pip install duckdb
python -m goldcheck compare --db exports/ --old old.sql --new new.sql
```

Question SQL runs on goldcheck's own in-memory copy, after DuckDB's `enable_external_access = false` and `lock_configuration = true`. That blocks file reads and writes, `COPY`, `ATTACH` and extension installs, and the setting cannot be switched back. Only one `SELECT` is accepted per query, and each has a timeout. Data loaded from files is held in memory, so very large folders need a sample. Queries must be valid DuckDB SQL: SQLite-only SQL, such as selecting an ungrouped column, is rejected as an error.

## Let a coding agent use it (MCP)

`python -m goldcheck mcp` serves one tool, `compare_queries(db_path, trusted_sql, sql, ordered?, shrink?, max_seconds?)`, over MCP stdio. It returns whether the results match. When they don't, it also returns the counterexample rows, both results on them, the clause that causes the difference and a script that recreates the rows. The agent can repair its query and call again. For Claude Code:

```
claude mcp add goldcheck -- python -m goldcheck mcp
```

Run it from the goldcheck folder, or install it so `python -m goldcheck` resolves. The same read-only, SELECT-only rules apply.

## How shrinking works

goldcheck copies the database file to a private temporary file. It finds which tables the two queries read from SQLite's own query plan, and deletes rows from those tables in shrinking chunks (delta debugging). Every deletion runs inside a SAVEPOINT and is kept only if both queries still run and still disagree. It stops when no single row can go, or after 60 s (`--shrink-seconds`), and the report says which.

First it tries one guess: rows equal to a text literal in either query (e.g. the post titled "Analysing wind data with R") plus rows linked to them by declared foreign keys. The guess is verified like any other step. On large databases it turns minutes into seconds.

- Your database file is only read. Question SQL runs under a SELECT-only authorizer with a timeout, on the copy as well.
- Declared foreign keys never gain a new dangling reference, and rows that kept rows point at are exported too. Undeclared relationships are not protected.
- Only rows are removed, never values changed. The working copy needs free disk space equal to the database size.

## Measured

| Data | Result |
| --- | --- |
| Demo, 4 silent failures | 1–3 rows each, each blamed on one clause and confirmed on the full database |
| 51 mechanical mutants of the demo's gold queries | 51/51 shrank to ≤8 rows (median 3) in ≤0.02 s |
| BIRD dev, 200 real ChatGPT errors (two random samples, one never seen during development) | 199/200 shrank to ≤10 real rows, all proven minimal, median 2 rows in 0.25 s, from tables of up to 1,067,081 rows; cause pinned to 1–2 clauses in 66% |

Reproduce: `python benchmarks/mutants.py`; for BIRD see `benchmarks/README.md`.

## How it compares

- **[VeriEQL](https://github.com/VeriEQL/VeriEQL) and [SpotIt+](https://github.com/ai-ar-research/SpotIt-plus)** build counterexamples with an SMT solver. They can find differences your data never triggers, which goldcheck cannot. Their rows are synthetic, they need a custom Z3 build (SpotIt+ also uses an LLM for constraint mining), and some queries cannot be encoded (SpotIt+ reports 3–7% on BIRD). goldcheck uses real rows, runs any SELECT SQLite runs, and needs only Python.
- **Result diff tools** (DataComPy, `assertDataFrameEqual`, Datafold) show *that* outputs differ, on all rows. goldcheck shrinks the *input* to the few rows that cause the difference and names the clause.

## What "correct" means

Results match when they contain the same rows, ignoring column names, column order, extra columns in the checked query, and numeric differences beyond six significant digits. Order matters only when `ordered` is set. See `product/RELIABILITY_CONTRACT.md`.

## Limits

- SQLite and DuckDB (including Parquet, CSV, JSON). For a warehouse, export a sample. Foreign keys are only respected in SQLite.
- A pass means "matches the trusted query on this database", not "correct everywhere".
- Clause blame swaps one clause at a time. Queries that differ in several clauses at once, or that use top-level `WITH` or `UNION`, get a counterexample but no single clause.
- Tables `WITHOUT ROWID` cannot be shrunk.

## Privacy and safety

No network calls, no telemetry. `--agent-cmd` runs a command you supply, with your permissions. Exported fixtures and reports contain real rows from your database. Details: `product/THREAT_MODEL.md`.

## Tests

```
python -m unittest discover -s tests
```

MIT licence. To remove it, delete this folder and any reports, `goldcheck-demo/` or exported test folders it wrote. It stores nothing elsewhere.
