#!/usr/bin/env bash
set -euo pipefail

INSTALL_DIR="/opt/yt-tg-bot"
DOWNLOAD_DIR="${DOWNLOAD_DIR:-/opt/yt-downloads}"
BOT_TOKEN="${BOT_TOKEN:-}"

if [ -z "$BOT_TOKEN" ]; then
  read -rp "请输入 BOT_TOKEN: " BOT_TOKEN
fi

apt-get update
apt-get install -y python3 python3-venv python3-pip ffmpeg nodejs git

mkdir -p "$INSTALL_DIR" "$DOWNLOAD_DIR"
cp bot.py "$INSTALL_DIR"/
cp .env.example "$INSTALL_DIR"/.env
sed -i "s|^BOT_TOKEN=.*|BOT_TOKEN=$BOT_TOKEN|" "$INSTALL_DIR/.env"
sed -i "s|^DOWNLOAD_DIR=.*|DOWNLOAD_DIR=$DOWNLOAD_DIR|" "$INSTALL_DIR/.env"

cd "$INSTALL_DIR"
python3 -m venv venv
./venv/bin/pip install --upgrade pip
./venv/bin/pip install python-telegram-bot "yt-dlp[default]" python-dotenv

id -u ytbot >/dev/null 2>&1 || useradd -r -s /usr/sbin/nologin ytbot
chown -R ytbot:ytbot "$INSTALL_DIR" "$DOWNLOAD_DIR"

cp yt-tg-bot.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now yt-tg-bot
systemctl status yt-tg-bot --no-pager
