# Contributing

Thanks for helping make BabelTower boring, inspectable, and useful.

## How To Contribute

- Open an issue for bugs, protocol questions, and proposed behavior changes.
- Keep pull requests focused and explain protocol-visible effects.
- Add or update tests for behavior changes.
- Run `make lint` and `make test` before submitting.
- Avoid adding dependencies unless they clearly reduce complexity.

## Development Setup

```sh
python -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
docker compose up -d
make migrate
make test
make lint
```

## Protocol Changes

Protocol changes should update `PROTOCOL.md`, schemas, tests, and any reference agent behavior. Backwards-compatible additions may stay under `/v1`; breaking changes should target a future version path.

## Code Of Conduct

Participation in this project is governed by our [Code of Conduct](CODE_OF_CONDUCT.md): be direct, kind, and specific. No harassment, hate, threats, sexualized abuse, doxxing, or sustained disruption. The maintainer may close issues, delete comments, or block contributors who make the project unsafe or unproductive. Report concerns privately to security@babel-tower.com.
