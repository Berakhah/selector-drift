# Credentials and access

## The one sentence for the first call

"We would rather never hold your password: the default is that you log in yourself and hand us the
session, and if you need us to hold a second factor, the answer is no — storing your authenticator
seed would defeat the reason you turned MFA on, and it may breach your own security policy even if
you ask us to."

## Preference order

| # | Posture (`credential_posture`) | What we hold | Second factor |
|---|---|---|---|
| 1 | `session_injection` — **the default offer** | a Playwright storage state you export after logging in, mode 0600, cookies for your portal's origin only | you complete MFA when you export the session |
| 2 | `client_broker` | a bearer token for **your** broker endpoint; nothing else | your broker returns a fresh code per run; the seed never leaves you |
| 3 | delegated access | whatever your identity provider grants a service identity | governed by your IdP |
| 4 | `encrypted_store` — **last resort** | username and password, envelope-encrypted | none: we cannot hold one |

The posture is a field on the target's scope record, so it is part of what the client authorised
(and of the record's SHA-256). An unknown posture refuses to load; a posture with no provider
refuses to run; neither ever falls back to a weaker one. A provider registers once and cannot be
replaced.

`encrypted_store` refuses to store or read anything until your signed acknowledgement is on file.
We record its SHA-256, who recorded it and when, twice: in the audit log, and in an append-only
history sealed under your data key — a second acknowledgement is added beside the first, never in
place of it, and a history edited or forged without the key is refused. So the acknowledgement that
was in force is checkable later.

Whatever the posture, only cookies for your portal's authorised host reach the browser: a cookie
for another domain is dropped, a parent-domain cookie is narrowed to the exact host, an expired
one is dropped, and an answer left with nothing usable is refused rather than run logged out.

## The client broker

We call two endpoints under the https URL you give us, with `?target=<name>` and the token in an
`Authorization: Bearer` header:

- `GET <url>/credentials` → `{"cookies": [...]}` or `{"username": "...", "password": "..."}`
- `GET <url>/totp` → `{"code": "123456"}` (6–8 digits)

The URL must be `https`, with no user-info, query string or fragment; redirects are not followed.
We keep nothing the broker returns beyond the run that asked for it — no file, no cache.
Configuration is per target: `SD_BROKER_URL_<TARGET>` and `SD_BROKER_TOKEN_<TARGET>`.

## What we never do

- **Store a TOTP seed.** No field, column or setting for one exists anywhere in the product, and a
  test fails the build if one is added.
- Circumvent authentication or defeat anti-bot measures. A portal that needs evasion is out of scope.
- Write into your portal. The change-set export is a file you review and import yourself.
- Log a credential. Passwords are `SecretStr`; cookies are excluded from every representation;
  tokens travel in an `Authorization` header and never in a URL, a log line or an error message.

## How stored credentials are protected

A key-encryption key (KEK) lives only in the worker's environment (`SD_CREDENTIAL_KEK`, 64 hex
characters). It wraps a per-client 256-bit data key; the data key seals each target's credential
with AES-GCM, bound to that client and target, so a file copied to another client or target does
not decrypt. The design supports rotating the KEK: only the data-key file is wrapped by it, so
rotation re-wraps one small file per client. No rotation tool exists yet. Files are written
atomically, through an fsync'd temp file, with owner-only permissions; the data key is created
once, never left truncated by a crash. A key, credential or acknowledgement file that group or
others can read is refused.
