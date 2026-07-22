from fastapi import Header, HTTPException

from app.config import settings


def require_internal_api_token(authorization: str | None = Header(default=None)) -> None:
    expected = settings.internal_api_token.strip()
    if not expected:
        raise HTTPException(status_code=503, detail="Internal API is not configured")

    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")

    token = authorization[7:].strip()
    if token != expected:
        raise HTTPException(status_code=403, detail="Invalid token")
