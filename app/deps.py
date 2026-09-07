"""FastAPI dependencies.

Both the GitHub client and the event store are created once during the app
lifespan and hung off ``app.state``; these accessors are what routers depend
on, which is also the seam tests override.
"""

from __future__ import annotations

from fastapi import Request

from .config import Settings
from .github_client import GitHubClient
from .store import EventStore


def get_settings_dep(request: Request) -> Settings:
    return request.app.state.settings


def get_github(request: Request) -> GitHubClient:
    return request.app.state.github


def get_store(request: Request) -> EventStore:
    return request.app.state.store
