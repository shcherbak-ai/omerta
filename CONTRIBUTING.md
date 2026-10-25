# Contributing

## Branch Strategy

- **`dev`** is the default development branch: all pull requests target `dev`
- `main` is reserved for releases

## Setup

Requires Python 3.11+, [uv](https://docs.astral.sh/uv/), git 2.31+, and [Node.js](https://nodejs.org/) 22 or newer for the development tools that `package.json` lists: markdownlint, which a pre-commit hook runs, the stylesheet of the README preview, and the agents' browser. None of them is part of the package, and none runs a script when it is installed (`ignore-scripts` in `.npmrc`).

```bash
git clone https://github.com/shcherbak-ai/omerta.git
cd omerta
git checkout dev
uv sync
npm ci
uv run pre-commit install --hook-type pre-commit --hook-type commit-msg
```

## Pre-commit Hooks

Run automatically on every commit:

- **ruff**: linting and formatting
- **ty**: type checking ([Astral's type checker](https://docs.astral.sh/ty/))
- **deptry**: dependency declaration/import consistency
- **uv lock check**: `uv.lock` matches `pyproject.toml`
- **advisories**: a release enters `uv.lock` and `package-lock.json` only a week after its upload (`exclude-newer` in `pyproject.toml`, `min-release-age` in `.npmrc`), and no locked version, nor the uv that the workflows pin, has an advisory at [osv.dev](https://osv.dev), but for those listed with their reasons in `ACCEPTED_ADVISORIES` in [scripts/check_advisories.py](scripts/check_advisories.py). It asks osv.dev at each commit, as an advisory can come out at any time, so it can fail on a change that touches no dependency: commit with `SKIP=advisories` in front of the command, and the maintainer upgrades the dependencies. CI skips it
- **bandit**: security analysis
- **markdownlint**: Markdown linting, with the version of `package-lock.json` (`npm ci` installs it)
- **interrogate**: docstring coverage on `src/`
- **vulture**: dead-code detection on `src/`
- **omerta**: omerta itself, from the source, on the staged files
- **file hygiene**: `trailing-whitespace`, `end-of-file-fixer`, `check-yaml`, `check-toml`
- **claude hooks tests**: the tests of the Claude Code hook in `.claude/hooks/` and of the settings that start it, with `node --test`
- **commitizen**: commit messages follow Conventional Commits (`fix: …`, `feat: …`)

Manual run:

```bash
uv run pre-commit run --all-files
```

## Tests

```bash
uv run pytest -n 8 --cov=omerta --cov-report=term-missing
```

100% test coverage is required, on every platform and Python of the CI matrix. All new code must include tests. The tests run git in temporary repositories, cut off from your git configuration, and make no network connection.

## Code Style

- ruff format, line length 100
- ty (default rules; scoped to `src/` via `[tool.ty.src]`)
- `from __future__ import annotations` in every Python file
- Google-style docstrings
- The rules a change must keep are in [AGENTS.md](AGENTS.md).

## Pull Requests

1. Fork and branch from `dev`
2. All tests pass with 100% coverage
3. Pre-commit hooks pass
4. Submit PR to `dev`

## Dependency licenses

[LICENSES.md](LICENSES.md) is the report of the license check that CI runs on the package's own dependencies (`licenseal.yml`). Write it again in the change that moves a dependency:

```bash
uv run licenseal check --no-dev -f markdown -o LICENSES.md
```

## README and package description

`README.md` is for users of every language, since env files are in every ecosystem. `PYPI.md` is the package description (`project.readme` in `pyproject.toml`): keep it short, with absolute HTTPS links, and update it when the summary, the commands or the hook change.

The examples pin a release (`omerta@X.Y.Z`, `rev: vX.Y.Z`). `cz bump` moves the pins with the version, and a test fails when one names another version.

Look at a change as GitHub will show it before pushing it. `uv run inv readme-preview` serves `README.md` in GitHub's light theme at <http://127.0.0.1:9120/> and in its dark theme at <http://127.0.0.1:9121/> (`--port N` serves them at N and N+1):

- The HTML is what GitHub's Markdown API returns for the file, so what GitHub removes from a README is removed here too.
- The look comes from github-markdown-css, in GitHub's colours, each page in its theme whatever the browser is set to. The column is as wide as GitHub's at 1280 pixels and wider, and below 768.
- A reload sends the README to GitHub again when it has changed, once for both pages. GitHub allows 60 of those an hour without a token.

## Releases

The maintainer releases from `main`: `uv run cz bump` (the first release: `uv run cz bump 0.1.0`) rewrites the version fields, names the `## [Unreleased]` section of `CHANGELOG.md` after the new version with the day, and tags the commit; the publish workflow uploads the release to PyPI once CI passes on the tag. Write each change under `## [Unreleased]` in the change itself.
