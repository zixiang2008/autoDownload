#!/bin/sh
# 安全重启 yt-tg-bot：只杀真正的 python bot 进程（不会误杀 sh -c 命令行）
PIDS=$(ps -o pid,args | awk '$2 ~ /python$/ && $3 == "/opt/yt-tg-bot/bot.py" {print $1}')
echo "running bot pids: ${PIDS:-none}"
for p in $PIDS; do
  kill -9 "$p" 2>/dev/null
done
sleep 2
rm -f /run/yt-tg-bot.pid /run/yt-tg-bot.lock
rc-service yt-tg-bot zap >/dev/null 2>&1
rc-service yt-tg-bot start
sleep 6
rc-service yt-tg-bot status
echo "current procs:"
ps -o pid,args | awk '$2 ~ /python$/ && $3 == "/opt/yt-tg-bot/bot.py" {print $1, $2, $3}'
