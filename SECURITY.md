# Security

goldcheck runs SQL you did not write (an AI's, a colleague's) against your data. The safety model is in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#safety-model): read-only connections or private copies, single statements, timeouts, and on Postgres a READ ONLY transaction inside a rolled-back savepoint.

For chatbots and agents (MCP): start the server with named databases (`goldcheck mcp --db NAME=URL`) so the model never sees connection strings, keep passwords in `PGPASSWORD` or `~/.pgpass`, and give goldcheck a read-only database role.

Known limits:
- `--agent-cmd` runs a command you supply, with your permissions.
- On Postgres, checked SQL can still call functions that need no write access (for example `pg_terminate_backend` on your own sessions, or `dblink` if installed). Use a role that can only read the tables involved and create temporary tables.
- Reports, counterexample files and exported tests contain real rows from your database. Review them before sharing.

To report a vulnerability, open a private security advisory on the GitHub repository (Security tab → "Report a vulnerability"). Please don't put sensitive data in public issues.
