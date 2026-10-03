"""Server-rendered web UI (Jinja2 + HTMX). See docs/UI.md."""

from fastapi import FastAPI


def install(app: FastAPI) -> None:
    from . import (
        routes_api,
        routes_auth,
        routes_data,
        routes_memory,
        routes_oauth,
        routes_org,
        routes_plugins,
        routes_review,
        routes_settings,
    )
    from .deps import install_error_handlers

    install_error_handlers(app)
    for module in (
        routes_api,
        routes_auth,
        routes_memory,
        routes_review,
        routes_data,
        routes_oauth,
        routes_org,
        routes_plugins,
        routes_settings,
    ):
        app.include_router(module.router)
