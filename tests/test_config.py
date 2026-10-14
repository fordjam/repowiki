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


# --------------------------------------------------------------------------- effort presets

import json  # noqa: E402

from repowiki.config import PRESETS, Settings, preset, resolve_effort  # noqa: E402

TABLE = {
    "draft": (12, 32 * 1024, 50_000, False),
    "standard": (20, 64 * 1024, 150_000, True),
    "deep": (30, 128 * 1024, 400_000, True),
}
STRONG = {"cheap": "fake", "strong": "fake:strong"}


@pytest.mark.parametrize("effort", ["turbo", "Deep", "deep@2", "draft@", "", "@1"])
def test_unknown_preset_refuses(tmp_path: Path, effort: str) -> None:
    with pytest.raises(ValueError, match="unknown effort preset"):
        load_settings(tmp_path, {"effort": effort, **STRONG})


def test_unknown_preset_in_the_configuration_file_refuses(tmp_path: Path) -> None:
    (tmp_path / ".repowiki.toml").write_text('effort = "fast"\n')
    with pytest.raises(ValueError, match="unknown effort preset 'fast'"):
        load_settings(tmp_path)


@pytest.mark.parametrize("name", sorted(TABLE))
def test_presets_resolve_deterministically(tmp_path: Path, name: str) -> None:
    pages, context, cap, final_pass = TABLE[name]
    first = load_settings(tmp_path, {"effort": name, **STRONG})
    again = load_settings(tmp_path, {"effort": f"{name}@1", **STRONG})
    for s in (first, again):
        assert (s.max_pages, s.context_chars, s.context_bytes, s.budget_tokens, s.two_pass) == (
            pages, context, context, cap, final_pass
        )
    assert first.effective() == again.effective() == load_settings(tmp_path, {"effort": name, **STRONG}).effective()
    assert f"effort: {name} (presets v1; sets limits only, not a quality guarantee)" in first.effective()
    assert (PRESETS[name].version, preset(name)) == (1, PRESETS[name])


def test_effort_from_the_configuration_file_matches_the_flag(tmp_path: Path) -> None:
    (tmp_path / ".repowiki.toml").write_text('effort = "standard"\n[models]\nmodel = "fake"\n')
    from_file = load_settings(tmp_path)
    from_flag = load_settings(tmp_path, {"effort": "standard"})
    assert from_file.effective() == from_flag.effective()
    assert (from_file.max_pages, from_file.context_chars, from_file.budget_tokens) == (20, 65_536, 150_000)


def test_explicit_settings_tighten_a_preset(tmp_path: Path) -> None:
    s = load_settings(tmp_path, {"effort": "standard", "max_pages": 8, "context_chars": 10_000,
                                 "budget_tokens": 90_000, **STRONG})  # fmt: skip
    assert (s.max_pages, s.context_chars, s.budget_tokens) == (8, 10_000, 90_000)
    assert s.effective()[1:4] == [
        "pages: 4 to 8",
        "source context: at most 10,000 characters and 65,536 UTF-8 bytes per page prompt",
        "run cap: 90,000 estimated input tokens (checked before every call)",
    ]


@pytest.mark.parametrize("name, value", [("max_pages", 13), ("context_chars", 32 * 1024 + 1),
                                         ("budget_tokens", 50_001)])  # fmt: skip
def test_explicit_settings_cannot_widen_a_preset(tmp_path: Path, name: str, value: int) -> None:
    with pytest.raises(ValueError, match="can only tighten"):
        load_settings(tmp_path, {"effort": "draft", name: value, "cheap": "fake"})
    (tmp_path / ".repowiki.toml").write_text(f'effort = "draft"\n{name} = {value}\n')
    with pytest.raises(ValueError, match="can only tighten"):
        load_settings(tmp_path)


@pytest.mark.parametrize("models", [{"cheap": "fake"}, {"cheap": "fake", "strong": "fake"}, {"strong": "fake"}])
def test_missing_strong_model_fails_deep(tmp_path: Path, models: dict) -> None:
    with pytest.raises(ValueError, match="deep preset needs an approved strong model"):
        load_settings(tmp_path, {"effort": "deep", **models})



@pytest.mark.parametrize("models", [{"cheap": "fake"}, {"cheap": "fake", "strong": "fake"}, {"strong": "fake"}])
def test_standard_runs_an_approved_final_pass_on_the_one_configured_model(tmp_path: Path, models: dict) -> None:
    s = load_settings(tmp_path, {"effort": "standard", **models})
    assert (s.cheap_spec, s.strong_spec, s.separate_strong, s.two_pass) == ("fake", "fake", False, True)
    assert "models: drafting fake; final pass fake (the drafting model)" in s.effective()
    cheap, strong = s.describe_runners()
    assert cheap is not strong and (cheap.label, strong.label) == ("fake", "fake")  # a real second pass
    assert load_settings(tmp_path, {"effort": "standard", **STRONG}).effective()[4] == (
        "models: drafting fake; final pass fake:strong"
    )


def test_draft_is_one_pass_with_the_drafting_model(tmp_path: Path) -> None:
    s = load_settings(tmp_path, {"effort": "draft", **STRONG})
    assert (s.cheap_spec, s.strong_spec, s.two_pass) == ("fake", "fake", False)
    assert "models: drafting fake; final pass none (one pass)" in s.effective()


def test_settings_built_directly_must_respect_the_preset() -> None:
    with pytest.raises(ValueError, match="within the draft preset"):
        Settings(effort="draft", cheap="fake")  # default 60,000-character context is above draft's ceiling
    assert Settings(**resolve_effort({"effort": "draft", "cheap": "fake"})).max_pages == 12


def _steering(root: Path, pages: int) -> None:
    data = {
        "version": 1,
        "repo_notes": [],
        "pages": [
            {"id": f"p{n}", "file": f"p{n}.md", "title": f"Page {n}", "purpose": f"Purpose {n}."}
            for n in range(1, pages + 1)
        ],
    }
    (root / ".repowiki").mkdir(exist_ok=True)
    (root / ".repowiki/wiki.json").write_text(json.dumps(data))


def test_exact_steering_page_count_is_never_trimmed(tmp_path: Path) -> None:
    _steering(tmp_path, 13)
    with pytest.raises(ValueError, match="exactly 13 pages.*never trimmed"):
        load_settings(tmp_path, {"effort": "draft", "cheap": "fake"})
    with pytest.raises(ValueError, match="never trimmed"):
        load_settings(tmp_path, {"effort": "standard", "max_pages": 12, "cheap": "fake"})  # a tighter flag too
    s = load_settings(tmp_path, {"effort": "standard", "cheap": "fake"})
    assert s.max_pages == 20 and len(s.load_steering(tmp_path).pages) == 13
