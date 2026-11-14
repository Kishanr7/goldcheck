# goldcheck

**Find out how often your data chatbot returns a wrong number — before your users do.**

> Status: `PORTFOLIO-ONLY` · `UNVALIDATED`. Built as an engineering portfolio piece. No claim is made that anyone needs it, uses it, or prefers it to alternatives. It has been tested by its author's AI agent on Linux only.

A "chat with your data" agent can write SQL that runs cleanly and still gives the wrong answer. goldcheck takes a list of questions with known-good SQL, runs your agent's SQL next to it on the same database, and tells you which answers were right, which failed loudly, and which were **silently wrong**.

- Works with an agent you already have: give it a file of the agent's SQL, or a command to call.
- Scoring is deterministic. No model, no API key, no account.
- Python standard library only. Nothing to install, nothing leaves your computer.

## Try it in one minute

Needs Python 3.10+.

```
python -m goldcheck demo
```

This builds a small fictional shop database, scores a deliberately careless agent, and writes `goldcheck-demo/report.html`. Expected output:

```
7/12 correct (58%, 95% interval 32-81%)
4 ran cleanly but returned the wrong result; 1 failed visibly.
```

Open the report: the four silent failures are expanded, each showing the expected query and result beside the agent's.

## Score your own agent

1. Write questions with gold SQL (`questions.json`):
   ```json
   [{"id": "q1", "question": "How many customers?", "gold_sql": "SELECT COUNT(*) FROM customers", "category": "counting"}]
   ```
   Add `"ordered": true` when row order matters.
2. Collect your agent's SQL as JSON Lines (`answers.jsonl`), one `{"id": "q1", "sql": "..."}` per line — or let goldcheck call it:
   ```
   python -m goldcheck run --db shop.sqlite --questions questions.json --answers answers.jsonl
   python -m goldcheck run --db shop.sqlite --questions questions.json --agent-cmd "my-agent --sql {question}"
   ```
3. Use `--min-accuracy 0.8` to fail a CI job below a threshold, `--json` for machine output.

## What "correct" means

Both queries run read-only on the same SQLite file. Results match when they contain the same rows, ignoring column names, column order, extra agent columns, and numeric differences beyond six significant digits. See `product/RELIABILITY_CONTRACT.md` for the exact rules and known blind spots.

## Limits

- SQLite databases only in this version. Export a sample of your warehouse to SQLite to use it.
- A pass means "matches the gold answer", so wrong gold SQL gives wrong scores.
- Small question sets give wide intervals; the report shows the interval so the number is not over-read.
- Windows and macOS are untested.

## Privacy and safety

No network calls, no telemetry. Agent SQL is confined to reading: the database is opened read-only and an authorizer rejects everything except `SELECT`. Queries are stopped after 5 seconds. `--agent-cmd` runs a command you supply, with your permissions. Details: `product/THREAT_MODEL.md`.

## Remove it

Delete this folder and any `goldcheck-demo/` folder or report files it wrote. It stores nothing elsewhere.

## Tests

```
python -m unittest discover -s tests
```

MIT licence.
