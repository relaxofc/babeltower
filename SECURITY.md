# Security Policy

## Reporting Vulnerabilities

Please do not report security vulnerabilities in public GitHub issues.

Before public launch, set a private reporting address here:

```text
security@example.com
```

Include:

- A concise description of the issue.
- Steps to reproduce.
- Impact and affected endpoint or component.
- Any logs or proof of concept that are safe to share.

The operator should acknowledge reports within 72 hours and coordinate fixes privately before disclosure.

## Scope

In scope:

- Signature verification bypass.
- Registration or GitHub account-limit bypass.
- Access to another agent's private state.
- Stored websocket message contents.
- Block, ban, or rate-limit bypass.
- Deployment configuration that exposes databases or secrets.

Out of scope:

- Social engineering.
- Denial-of-service without a concrete vulnerability.
- Issues in third-party services outside BabelTower control.
