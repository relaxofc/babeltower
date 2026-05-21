#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run this script as root on the production Ubuntu host." >&2
  exit 1
fi

apt-get update
apt-get install -y \
  ca-certificates \
  curl \
  debian-keyring \
  debian-archive-keyring \
  apt-transport-https \
  gnupg \
  lsb-release \
  rclone \
  ufw \
  unattended-upgrades

# Docker CE official apt repo (Ubuntu's docker.io ships without the compose
# plugin we rely on, and mixing both packages is unreliable).
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
  | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
chmod a+r /etc/apt/keyrings/docker.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" \
  > /etc/apt/sources.list.d/docker.list

# Caddy Cloudsmith repo (Caddy is not in Ubuntu's default repos).
curl -fsSL https://dl.cloudsmith.io/public/caddy/stable/gpg.key \
  | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -fsSL https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt \
  > /etc/apt/sources.list.d/caddy-stable.list

apt-get update
apt-get install -y \
  docker-ce \
  docker-ce-cli \
  containerd.io \
  docker-buildx-plugin \
  docker-compose-plugin \
  caddy

systemctl enable --now docker
systemctl enable --now caddy

ufw allow OpenSSH
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable

dpkg-reconfigure --priority=low unattended-upgrades

echo "Bootstrap complete. Next: clone the repo, copy .env, install deploy/Caddyfile, and run docker compose."
