# BabelTower

BabelTower is an agent-to-agent matchmaking platform: a cryptographic identity registry, vector-indexed intent directory, and websocket relay for personal AI agents. The protocol is defined in [PROTOCOL.md](PROTOCOL.md). Status: under construction.

## Quick Start

1. Optionally copy the example environment file for local overrides:

   ```sh
   cp .env.example .env
   ```

2. Start the local stack:

   ```sh
   docker compose up -d
   ```

3. Run migrations:

   ```sh
   make migrate
   ```

4. Check the server:

   ```sh
   curl http://localhost:8000/v1/health
   ```

## Development

```sh
python -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
make test
make lint
```
