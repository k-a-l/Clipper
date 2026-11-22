# Contributing

Thanks for helping! Bug reports, fixes and features are all welcome.

## Set up

```bash
git clone https://github.com/k-a-l/clipper && cd clipper
uv venv --python 3.12
uv pip install -e ".[dev]"
uv run pytest          # needs ffmpeg on PATH for the render tests (they're skipped otherwise)
uv run ruff check clipper tests
```

Run your copy with `uv run clipper ui --open --no-publish`. Use `CLIPPER_HOME=/tmp/clipper-dev`
to keep test data apart from a real install.

## Guidelines

- Keep pull requests focused; describe what changed and how you tested it.
- Add a test for logic changes (`tests/`). The render test uses a synthetic FFmpeg source, so
  copy that pattern rather than committing media files.
- **Never commit secrets, tokens, cookies, `.env` or anything from `storage/`.** CI runs a secret
  scanner (gitleaks) on every push.
- New dependencies: prefer well-maintained libraries and add them to `pyproject.toml` and
  `THIRD_PARTY_COMPONENTS.md` (name, license, why). GPL tools must stay separate programs.
- Code that runs on one OS only needs a fallback for the others (CI tests macOS, Windows and Linux).

## Releasing (maintainers)

1. Bump `version` in `pyproject.toml`, commit.
2. `git tag v0.x.y && git push --tags`. The *Release* workflow builds the package, creates the
   GitHub Release and publishes `ghcr.io/k-a-l/clipper`.
