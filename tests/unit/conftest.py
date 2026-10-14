from __future__ import annotations

import pytest

from posta_mcp import attachments


@pytest.fixture
def no_windows_attributes(monkeypatch):
    """On Windows ``tmp_path`` lives under the hidden ``AppData`` folder, which attachments
    refuse; tests that attach files from ``tmp_path`` ignore the Windows hidden attribute."""
    monkeypatch.setattr(attachments, "_attributes", lambda path: 0)
