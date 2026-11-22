import os

import pytest


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Every test gets an empty Clipper home and no stray .env from the developer's machine."""
    home = tmp_path / "clipper-home"
    home.mkdir()
    monkeypatch.setenv("CLIPPER_HOME", str(home))
    monkeypatch.chdir(tmp_path)
    for key in list(os.environ):
        if key.startswith(("YOUTUBE_", "FACEBOOK_", "TWITCH_", "SOURCES", "TOKEN_ENCRYPTION")):
            monkeypatch.delenv(key, raising=False)
    return home
