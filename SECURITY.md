# Security policy

Selector Drift drives a real browser against client portals with the client's credentials,
stores redacted evidence, and lands extracted data. Treat any report touching the scope gate,
credential handling, redaction or the validation gate as high priority.

## Reporting a vulnerability

Report it privately through GitHub
([Security → Report a vulnerability](https://github.com/Berakhah/selector-drift/security/advisories/new))
with a description, steps to reproduce, and impact. Do not open a public issue for a suspected
vulnerability. I aim to acknowledge within 5 business days. This is a one-person project, so
there is no on-call rota behind that number.

Only `main` is supported. There are no maintained release branches or backports.

## Scope highlights

- **Scope gate.** A run that starts against a target with no scope record, an expired
  authorisation, or above its rate limit is critical.
- **Credentials.** A credential in plaintext at rest, in a log line, or in a screenshot or DOM
  snapshot is critical.
- **Validation gate.** A healed selector that writes data without passing the gate, or a
  quarantined run that lands, is critical. A plausible-but-wrong heal that the gate accepts
  is in scope too: send the mutation that causes it.
- **Browser.** A page that escapes the browser container, or a write request (anything but
  GET, HEAD or OPTIONS) that reaches a portal after login.

## Out of scope

- Findings that depend on the parked HTTP API in open PR #1; it is not on `main` and is not
  deployed anywhere.
- The test bench portal (`testbench/`) is deliberately mutating and has demo-only login. Bugs
  in it are ordinary issues unless they break out of its container.
