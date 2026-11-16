# Contributing

Thanks for looking. goldcheck is a portfolio project, but issues and pull requests are welcome.

- Python 3.10+, standard library only in the core. DuckDB and psycopg stay optional.
- Run `python -m unittest discover -s tests` before sending a change. Add a test for every behaviour change, and a safety test for anything that touches query execution.
- If you change `report.py`, run `node tools/xbrowser/report-test.js` (needs `npm i -D playwright@1.61.1` and `npx playwright install chromium webkit`). All 11 setups must pass.
- Use fictional or public data only in tests and examples.
- Be kind and assume good intent.
