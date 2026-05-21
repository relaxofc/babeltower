# Agent Instructions

- Read `PROTOCOL.md` before writing or modifying code. It is authoritative.
- If implementation and protocol disagree, change the implementation.
- Prefer test-first development for endpoints and major functions.
- Do not merge untested code to `main`.
- If a requirement is ambiguous, stop and ask instead of inventing scope.
- Keep commits small and imperative, for example `feat: add intent embedding pipeline`.
- Use FastAPI, SQLAlchemy 2.0 async, Alembic, Pydantic v2, pgvector, Redis, and FastAPI websockets unless explicitly approved otherwise.
- Comments should explain why, not what.

