"""Optional HTTP adapter (extra ``http``). The core never imports this package."""

from .router import create_health_router, create_router

__all__ = ["create_health_router", "create_router"]
