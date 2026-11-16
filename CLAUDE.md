# Working on goldcheck

- This repository is **public**. Everything committed (files, history, commit messages) is world-readable.
- Before every commit run `git status --short` and read the list. Never commit personal details, local paths, names of other projects, or generated output (reports, demo folders).
- Only `main` is pushed.
- Tests: `python -m unittest discover -s tests` (DuckDB and Postgres tests need their optional drivers). HTML report: `node tools/xbrowser/report-test.js`.
- Private planning notes live outside git in `private/` next to the main checkout, if present. Read `private/CLAUDE.md` for the standing rules.
