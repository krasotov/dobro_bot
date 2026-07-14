#!/usr/bin/env python3
import os
import paramiko

HOST = os.environ.get("DEPLOY_HOST", "201.51.6.233")
USER = os.environ.get("DEPLOY_USER", "root")
PASSWORD = os.environ.get("DEPLOY_PASSWORD", "")

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect(HOST, username=USER, password=PASSWORD, timeout=30)

cmds = [
    "cd /opt/max_bot && docker compose ps",
    "cd /opt/max_bot && docker compose up -d db nginx 2>/dev/null || true",
    "docker ps --format '{{.Names}}' | grep max_bot-app",
    "docker cp /opt/max_bot/app max_bot-app-1:/app/",
    "docker cp /opt/max_bot/.env max_bot-app-1:/app/.env 2>/dev/null || true",
    "cd /opt/max_bot && docker compose restart app",
    "sleep 5",
    "curl -sf https://bot.xn--80agc6alen.xn--p1ai/health",
    "docker logs max_bot-app-1 2>&1 | tail -15",
]

for cmd in cmds:
    print(f"$ {cmd}")
    _, stdout, stderr = ssh.exec_command(cmd, timeout=120)
    out = stdout.read().decode()
    err = stderr.read().decode()
    if out:
        print(out[-3000:])
    if err:
        print(err[-1000:])

ssh.close()
