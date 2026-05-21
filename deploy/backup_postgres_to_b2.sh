#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-/opt/babeltower}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/babeltower}"
POSTGRES_USER="${POSTGRES_USER:-babeltower}"
POSTGRES_DB="${POSTGRES_DB:-babeltower}"
B2_RCLONE_REMOTE="${B2_RCLONE_REMOTE:?Set B2_RCLONE_REMOTE to an rclone Backblaze B2 target, for example b2:babeltower-backups/postgres}"

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
backup_file="${BACKUP_DIR}/babeltower-${timestamp}.dump"

mkdir -p "${BACKUP_DIR}"
cd "${PROJECT_DIR}"

docker compose -f docker-compose.prod.yml exec -T db \
  pg_dump -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" -Fc > "${backup_file}"

rclone copyto "${backup_file}" "${B2_RCLONE_REMOTE}/$(basename "${backup_file}")"

find "${BACKUP_DIR}" -type f -name 'babeltower-*.dump' -mtime +7 -delete

echo "Uploaded ${backup_file} to ${B2_RCLONE_REMOTE}"
