#!/usr/bin/env python3
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.max_client import max_client


async def main() -> None:
    try:
        data = await max_client._request("GET", "/chats", params={"count": 100})
        print(json.dumps(data, ensure_ascii=False, indent=2))
    except Exception as exc:
        print("ERROR:", exc, file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
