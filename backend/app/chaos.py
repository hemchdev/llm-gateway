import random

from fastapi import HTTPException

from .config import Settings


def maybe_inject_failure(settings: Settings) -> None:
    if not settings.chaos_enabled:
        return
    if random.random() < settings.chaos_error_rate:
        raise HTTPException(status_code=503, detail="Injected chaos failure")
