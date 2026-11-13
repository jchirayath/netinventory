# Security policy

## Reporting a vulnerability

Please report security issues **privately**, through GitHub: **Security → Report a
vulnerability** on this repository. Don't open a public issue.

Include what you found, how to reproduce it, and the impact you expect. Reports are
acknowledged within 3 business days, with a first assessment within 7 days.

## Fix times

| Severity | Fixed within |
|---|---|
| Critical (or actively exploited) | 7 days |
| High | 30 days |
| Medium | 90 days |
| Low | the next planned change |

## Supported versions

Only `main` receives fixes. The Raspberry Pi copy updates itself from `main` nightly;
the web copy is updated with `engine/deploy-betty.sh`.

## Scope

How the copies talk to each other, and what protects each boundary, is in
[docs/threat-model.md](docs/threat-model.md).
