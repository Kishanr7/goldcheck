# Reliability contract

| Behaviour | Status |
| --- | --- |
| Same inputs give the same verdicts and report | Guaranteed (deterministic; tested) |
| Agent SQL cannot modify the database or run non-SELECT statements | Guaranteed for SQLite via read-only open + authorizer (tested) |
| A query running longer than the timeout is stopped | Guaranteed (tested) |
| Results equal up to column names, column order, extra agent columns, 6 significant digits | Treated as a pass (tested) |
| NULL vs 0, text "5" vs number 5 | Treated as different (tested for NULL) |
| Row order | Checked only when the question says `ordered` |
| Two expected columns with identical value sets but different row pairing | Caught in the common case (tested); a greedy column match could mis-pair when several columns hold identical multisets — known blind spot |
| A pass means the agent is right | **Not guaranteed** — only that it matches the gold SQL. Gold quality is the user's responsibility |
| The pass rate generalises to real questions | **Not guaranteed** — depends on how representative the question set is; the 95% Wilson interval covers sampling noise only |

Nothing here is probabilistic. The agent under test is the only uncertain component, and goldcheck never calls a model itself. If the agent is unavailable, its answers are recorded as "no answer" and the run still completes.
