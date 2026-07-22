#!/usr/bin/env python3
"""Set INTERNAL_API_TOKEN in /opt/max_bot/.env and rebuild app container."""

import os
import sys

import paramiko

HOST = os.environ.get("DEPLOY_HOST", "201.51.6.233")
USER = os.environ.get("DEPLOY_USER", "root")
PASSWORD = os.environ.get("DEPLOY_PASSWORD", "")
TOKEN = os.environ.get("INTERNAL_API_TOKEN", "")
ENV_PATH = "/opt/max_bot/.env"


def main() -> int:
    if not PASSWORD or not TOKEN:
        print("Need DEPLOY_PASSWORD and INTERNAL_API_TOKEN", file=sys.stderr)
        return 1

    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(HOST, username=USER, password=PASSWORD, timeout=30)

    token_escaped = TOKEN.replace("'", "'\"'\"'")
    cmd = (
        f"grep -q '^INTERNAL_API_TOKEN=' {ENV_PATH} "
        f"&& sed -i 's/^INTERNAL_API_TOKEN=.*/INTERNAL_API_TOKEN={token_escaped}/' {ENV_PATH} "
        f"|| echo 'INTERNAL_API_TOKEN={token_escaped}' >> {ENV_PATH}"
    )
    for c in (
        cmd,
        "cd /opt/max_bot && docker compose up -d --build app",
        "sleep 8",
        f"curl -sf -H 'Authorization: Bearer {TOKEN}' "
        "https://bot.xn--80agc6alen.xn--p1ai/internal/v1/summary | head -c 200",
    ):
        print(f"$ {c[:120]}...")
        _, stdout, stderr = ssh.exec_command(c, timeout=600)
        out = stdout.read().decode()
        err = stderr.read().decode()
        code = stdout.channel.recv_exit_status()
        if out:
            print(out[-1500:])
        if err:
            print(err[-800:], file=sys.stderr)
        if code != 0 and "curl" not in c:
            ssh.close()
            return code

    ssh.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
