#!/usr/bin/env python3
"""Deploy project to VPS via SSH/SFTP."""

import os
import stat
import sys
import tarfile
import tempfile
from pathlib import Path

import paramiko

HOST = os.environ.get("DEPLOY_HOST", "201.51.6.233")
USER = os.environ.get("DEPLOY_USER", "root")
PASSWORD = os.environ.get("DEPLOY_PASSWORD", "")
REMOTE_DIR = "/opt/max_bot"
PROJECT_ROOT = Path(__file__).resolve().parent.parent

SKIP_DIRS = {".venv", "__pycache__", ".git", "pgdata", "node_modules"}
SKIP_FILES = {".DS_Store"}


def should_skip(path: Path) -> bool:
    parts = set(path.parts)
    if parts & SKIP_DIRS:
        return True
    if path.name in SKIP_FILES:
        return True
    return False


def create_archive() -> str:
    tmp = tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False)
    tmp.close()
    with tarfile.open(tmp.name, "w:gz") as tar:
        for item in PROJECT_ROOT.rglob("*"):
            if should_skip(item):
                continue
            rel = item.relative_to(PROJECT_ROOT)
            tar.add(item, arcname=str(rel))
    return tmp.name


def run(ssh: paramiko.SSHClient, cmd: str, timeout: int = 600) -> tuple[int, str, str]:
    print(f"$ {cmd}")
    stdin, stdout, stderr = ssh.exec_command(cmd, timeout=timeout)
    out = stdout.read().decode()
    err = stderr.read().decode()
    code = stdout.channel.recv_exit_status()
    if out:
        print(out[-4000:] if len(out) > 4000 else out)
    if err:
        print(err[-2000:] if len(err) > 2000 else err, file=sys.stderr)
    return code, out, err


def main() -> int:
    try:
        import paramiko  # noqa: F401
    except ImportError:
        os.system(f"{sys.executable} -m pip install -q paramiko")
        import paramiko  # noqa: F401

    archive = create_archive()
    print(f"Archive: {archive}")

    if not PASSWORD:
        print("Set DEPLOY_PASSWORD env var", file=sys.stderr)
        return 1

    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(HOST, username=USER, password=PASSWORD, timeout=30)

    run(ssh, f"mkdir -p {REMOTE_DIR}")

    sftp = ssh.open_sftp()
    remote_tar = "/tmp/max_bot_deploy.tar.gz"
    sftp.put(archive, remote_tar)
    sftp.close()
    os.unlink(archive)

    commands = [
        f"mkdir -p {REMOTE_DIR} && tar -xzf {remote_tar} -C {REMOTE_DIR}",
        f"rm -f {remote_tar}",
        "command -v docker >/dev/null || curl -fsSL https://get.docker.com | sh",
        "apt-get update -qq && apt-get install -y -qq certbot 2>/dev/null || true",
        "mkdir -p /var/www/certbot",
        # Stop anything on 80 for certbot
        f"cd {REMOTE_DIR} && docker compose down 2>/dev/null || true",
        (
            "if [ ! -f /etc/letsencrypt/live/bot.xn--80agc6alen.xn--p1ai/fullchain.pem ]; then "
            "certbot certonly --standalone -d bot.xn--80agc6alen.xn--p1ai "
            "--non-interactive --agree-tos --register-unsafely-without-email || "
            "certbot certonly --standalone -d bot.xn--80agc6alen.xn--p1ai "
            "--non-interactive --agree-tos -m admin@admspgo.ru; fi"
        ),
        f"cd {REMOTE_DIR} && docker compose up -d --build",
        "sleep 8",
        f"curl -sf http://127.0.0.1:8000/health || curl -sf https://bot.xn--80agc6alen.xn--p1ai/health",
        (
            "curl -sf -X POST https://bot.xn--80agc6alen.xn--p1ai/setup-webhook "
            "-H 'Content-Type: application/json' "
            f"-d '{{\"password\":\"{os.environ.get('ADMIN_PASSWORD', '')}\"}}'"
        ),
    ]

    for cmd in commands:
        code, _, err = run(ssh, cmd, timeout=900)
        if code != 0 and "docker compose" not in cmd and "certbot" not in cmd and "setup-webhook" not in cmd:
            print(f"Command failed with code {code}", file=sys.stderr)
            if "health" in cmd:
                print("Health check failed, continuing...", file=sys.stderr)
            else:
                ssh.close()
                return code

    ssh.close()
    print("\n=== DEPLOY COMPLETE ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
