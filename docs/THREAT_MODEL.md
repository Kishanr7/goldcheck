# Threat model

| Threat | Mitigation | Residual |
| --- | --- | --- |
| Agent SQL deletes or alters data | Database opened `mode=ro`; authorizer allows only SELECT, column reads, functions, recursion | None known for SQLite |
| Agent SQL reads other files (`ATTACH`) or changes settings (`PRAGMA`) | Denied by authorizer (tested) | None known |
| Runaway query exhausts CPU | Progress-handler timeout; 10,000-row cap | Memory use of a single huge row is unbounded |
| Malicious text in questions or results injects script into the report | All dynamic text HTML-escaped; report has no scripts (tested) | None known |
| Data leaves the machine | No network code at all; no telemetry | The report contains sample rows — treat it like the data |
| `--agent-cmd` runs something harmful | It is the user's own command, run without a shell; question text is passed as one argument | User must trust their own command |
| Sensitive data in bug reports | README and report footer say reports contain data rows | Human error |

No release-blocking issue is open. No credentials are handled.
