# Production Deployment

Phase 10 targets a single Hetzner Ubuntu host serving BabelTower at `https://babeltower.xyz` with Caddy TLS termination.

## Host Bootstrap

Run as root on the Hetzner host:

```sh
./deploy/bootstrap_hetzner.sh
```

The script installs Docker, the Docker Compose plugin, Caddy, rclone, UFW, and unattended OS security upgrades. It enables ports 22, 80, and 443 only.

## App Deploy

```sh
git clone https://github.com/relaxofc/babeltower.git /opt/babeltower
cd /opt/babeltower
cp .env.production.example .env
```

Edit `.env` with production secrets. The GitHub OAuth callback must be:

```text
https://babeltower.xyz/v1/register/oauth/callback
```

**Important:** `POSTGRES_PASSWORD` appears twice in `.env` — once directly,
and once embedded inside `DATABASE_URL`. Both copies must match exactly or
the API will fail to connect to Postgres. Pick a long random password and
paste it into both places.

Install the Caddy config (edit `deploy/Caddyfile` first to replace
`babeltower.xyz` with your actual domain):

```sh
cp deploy/Caddyfile /etc/caddy/Caddyfile
caddy fmt --overwrite /etc/caddy/Caddyfile
systemctl reload caddy
```

Start the stack:

```sh
docker compose -f docker-compose.prod.yml up -d --build
```

Verify:

```sh
curl -i https://babeltower.xyz/v1/health
curl -s https://babeltower.xyz/v1/server/info
```

## Backups

Configure an rclone Backblaze B2 remote, then install the cron file:

```sh
rclone config
cp deploy/babeltower-backup.cron /etc/cron.d/babeltower-backup
chmod 0644 /etc/cron.d/babeltower-backup
```

Run one backup manually before trusting the cron:

```sh
B2_RCLONE_REMOTE=b2:babeltower-backups/postgres ./deploy/backup_postgres_to_b2.sh
```

Phase 10 is complete only after `/v1/health` returns 200 over HTTPS, one daily backup succeeds, and the owner-review end-to-end production flow passes.
