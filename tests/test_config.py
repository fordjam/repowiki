"""Privacy configuration is explicit and rejects ambiguous inputs."""

from pathlib import Path

import pytest

from repowiki.config import load_settings


@pytest.mark.parametrize("text", [
    '[models]\nunknown = "fake"\n',
    'exclude = []\nexclude = []\n',
    '[privacy]\nvisibility = "private"\nunknown = true\n',
    '[privacy]\nvisibility = "public"\nallowlist = ["../outside"]\n',
    'policy_commit = "HEAD"\n',
])
def test_invalid_privacy_configuration(tmp_path: Path, text: str) -> None:
    (tmp_path / ".repowiki.toml").write_text(text)
    with pytest.raises(ValueError):
        load_settings(tmp_path)


@pytest.mark.parametrize("explicit", [False, True])
def test_symlinked_configuration_is_refused_before_reading(tmp_path: Path, explicit: bool) -> None:
    root = tmp_path / "clone"
    root.mkdir()
    target = tmp_path / "elsewhere.toml"
    target.write_text("exclude = []\n")
    (root / ".repowiki.toml").symlink_to(target)
    with pytest.raises(ValueError, match="symlink"):
        load_settings(root, path=target if explicit else None)
