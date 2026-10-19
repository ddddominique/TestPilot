"""Routine API tests never contact Redis; real Redis tests use a separate app."""

from unittest.mock import Mock

import pytest


@pytest.fixture(autouse=True)
def broker(monkeypatch):
    sender = Mock()
    monkeypatch.setattr("testpilot.api.routes.celery_app.send_task", sender)
    return sender
