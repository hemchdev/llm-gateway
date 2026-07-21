import base64
import hmac

from fastapi import HTTPException, Request

from .config import Settings, get_settings


def _bearer_token(request: Request) -> str | None:
    authorization = request.headers.get("authorization", "")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() == "bearer" and token:
        return token
    if scheme.lower() == "basic" and token:
        try:
            decoded = base64.b64decode(token).decode("utf-8")
        except ValueError:
            return None
        _, _, password = decoded.partition(":")
        return password or None
    return request.headers.get("x-api-key")


def require_token(request: Request, expected_token: str | None, realm: str) -> None:
    if not expected_token:
        raise HTTPException(status_code=503, detail=f"{realm} authentication is not configured")

    supplied_token = _bearer_token(request)
    if not supplied_token:
        raise HTTPException(status_code=401, detail="Unauthorized")
    if not hmac.compare_digest(supplied_token, expected_token):
        raise HTTPException(status_code=403, detail="Forbidden")


async def require_admin_auth(request: Request) -> None:
    settings: Settings = getattr(request.app.state, "settings", get_settings())
    require_token(request, settings.admin_token(), "admin")


async def require_metrics_auth(request: Request) -> None:
    settings: Settings = getattr(request.app.state, "settings", get_settings())
    require_token(request, settings.metrics_token(), "metrics")
