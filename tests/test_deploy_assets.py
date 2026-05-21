from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_caddyfile_matches_phase_10_requirements() -> None:
    caddyfile = (ROOT / "deploy" / "Caddyfile").read_text()

    assert "babeltower.xyz" in caddyfile
    assert "reverse_proxy localhost:8000" in caddyfile
    assert 'Strict-Transport-Security "max-age=31536000; includeSubDomains"' in caddyfile
    assert "X-Content-Type-Options nosniff" in caddyfile
    assert "Referrer-Policy strict-origin-when-cross-origin" in caddyfile


def test_production_compose_binds_api_to_localhost_and_runs_migrations() -> None:
    compose = (ROOT / "docker-compose.prod.yml").read_text()

    assert "127.0.0.1:8000:8000" in compose
    assert "alembic upgrade head" in compose
    assert "restart: unless-stopped" in compose
    assert "POSTGRES_PASSWORD is required in production" in compose


def test_backup_script_uploads_pg_dump_to_configured_b2_remote() -> None:
    backup_script = (ROOT / "deploy" / "backup_postgres_to_b2.sh").read_text()

    assert "pg_dump" in backup_script
    assert "docker-compose.prod.yml" in backup_script
    assert "B2_RCLONE_REMOTE" in backup_script
    assert "rclone copyto" in backup_script
