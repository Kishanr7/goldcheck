# The two rows that explain an AI's SQL mistake

*goldcheck write-up, November 2026. Portfolio project; numbers are reproducible with the scripts in `benchmarks/`.*

## The problem: wrong answers that don't look wrong

Ask a text-to-SQL model "How many single bonds are there?" against a chemistry database and it writes:

```sql
SELECT COUNT(*) FROM bond WHERE bond_type = 'single';
```

It runs, returns `0` and looks perfectly reasonable. It is wrong: in this database a single bond is stored as `'-'`. Nothing crashed, so nobody notices.

The usual way to catch this is to keep a known-correct "gold" query and compare results. That tells you *that* the answer is wrong. It doesn't tell you *why*, and on a table with hundreds of thousands of rows, the two result sets don't help much either.

## The idea: shrink the database, not the query

goldcheck runs the trusted query and the checked query side by side, and when they disagree it **deletes rows from a private copy of the database until no single row can be removed and the two queries still disagree**. This is delta debugging (Zeller and Hildebrandt, 2002), applied to the data instead of the code. What is left is the explanation. For the example above, it is one row:

| bond_id | molecule_id | bond_type |
|---|---|---|
| TR502_7_10 | TR502 | **-** |

Expected answer on this row: 1. The model's answer: 0. The fix is obvious at a glance.

goldcheck then swaps clauses between the two queries, one or two at a time, and re-runs them. If swapping in the trusted WHERE clause makes the results match, on the counterexample *and* on the full database, it says so: **"the difference comes from the WHERE clause."** Everything is executed, not guessed.

## Results on real model errors

[BIRD](https://bird-bench.github.io/) is a public text-to-SQL benchmark (CC BY-SA 4.0) with 11 real databases, some over a million rows. Its authors publish the SQL that ChatGPT wrote for all 1,534 dev questions. goldcheck's verdict agrees with BIRD's own scoring on 95.8% of them, and marks 879 as *silently* wrong: they ran, but returned the wrong result.

On two random samples of 100 of those 879 (one never used while developing the tool):

| | Fresh sample (seed 2468) | Second sample (seed 31337) |
|---|---|---|
| Shrunk to ≤10 real rows, proven 1-minimal | **100 / 100** | **99 / 100** |
| Median counterexample | 2 rows | 2 rows |
| Median time | 0.27 s | 0.23 s |
| Cause pinned to 1–2 clauses | 67 | 65 |
| …and confirmed on the full database | 45 | 38 |

The largest tables shrink just as well: 1,067,081 rows down to 4 in 9 seconds, and a Stack Exchange dump with 306,576 rows down to one comment, one post and one user in 2.6 seconds.

Getting there took three versions, each driven by the benchmark:

1. **Informative counterexamples.** The first version happily "explained" 14 of 100 errors with an *empty* database (one query returns NULL, the other 0). Now, when the trusted query returns data on the full database, the counterexample must too. Empty explanations dropped from 14 to 0.
2. **Guided first guesses.** Halving a million-row table one check at a time is slow when each check takes seconds. goldcheck first tries rows that match a text literal in either query, plus their foreign-key neighbours, then a random sample of 1,000, 100 or 10 rows. Every guess is verified like any other step, so it can only help.
3. **Cascading deletes.** On self-referencing tables (an answer post points at its question post), almost every deletion left a kept row pointing at a deleted one, so the safety check rejected it. Deleting a row together with the rows that point at it fixed every slow case.

## What the mistakes actually are

I read all 99 counterexamples from the first sample and classified them (details in `research/ERROR_TAXONOMY_2026-11-16.md`; one reader's judgement):

| Kind | Share |
|---|---|
| Didn't know how *this* database encodes things (`'single'` vs `'-'`, `'Euro'` vs `'EUR'`, a status code) | 42% |
| Misread the question or made things up | 23% |
| SQL logic slip (DISTINCT, join fan-out, NULLs, ties, LIMIT) | 14% |
| The benchmark's own gold answer is questionable | 11% |
| Formatting (rounding, extra columns) | 9% |

Only about one in seven is the "bad SQL" kind that better models will simply stop making. Half are about one database's conventions or a disputed answer key. That is exactly where a two-row counterexample helps most.

## How it compares

- **VeriEQL and SpotIt+** (VLDB 2024, 2026) generate counterexamples with an SMT solver. They can find differences that your data never triggers, which goldcheck cannot. But their rows are synthetic, they need a custom Z3 build (SpotIt+ also an LLM for constraint mining), and some queries can't be encoded.
- **Data-diff tools** (Datafold, DataComPy) show *that* outputs differ, across all rows. goldcheck shrinks the *input* to the few rows that cause the difference and names the clause.
- goldcheck's niche: real rows, any SQL the engine runs, clause-level cause, and zero dependencies for SQLite.

## Safety

Checked SQL is treated as untrusted. In SQLite it runs on a read-only connection with an authorizer that allows only reads. In DuckDB it runs on an in-memory copy with file and network access switched off and locked. In Postgres it runs as a single statement in a read-only transaction inside a savepoint that is always rolled back; shrinking works on session-private TEMP copies. Every query has a timeout. The test suite tries the usual escapes (`DELETE`, stacked statements, data-modifying CTEs, `ATTACH`, `COPY`, flipping settings) on every engine.

## Honest limits

- **Not yet used by a team in production.** Hands-on testing by the author, a data engineer, sharpened where it fits: it pays off when checking is automated (eval suites, query changes gated in CI, agents verifying their own SQL), rather than as a manual step after every assistant answer. goldcheck has the pieces for that (CI exit codes, regression-test export, an MCP server); the next step is putting them in front of teams that run those workflows.
- It can only show differences that the given data triggers.
- One model (2023) on one benchmark. Newer models make fewer mistakes, and different ones.

## Try it

```
pip install path/to/goldcheck
goldcheck demo --out gc-demo
```

Then run the `compare` command it prints. Engines: SQLite (nothing to install), DuckDB/Parquet/CSV/JSON (`[duckdb]`), Postgres (`[postgres]`).
