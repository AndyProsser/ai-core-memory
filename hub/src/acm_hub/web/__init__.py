"""Server-rendered web UI (Jinja2 + HTMX). See docs/UI.md."""

from fastapi import FastAPI


def install(app: FastAPI) -> None:
    from . import routes_auth, routes_data, routes_memory, routes_plugins, routes_review, routes_settings
    from .deps import install_error_handlers

    install_error_handlers(app)
    for module in (routes_auth, routes_memory, routes_review, routes_data, routes_plugins, routes_settings):
        app.include_router(module.router)
