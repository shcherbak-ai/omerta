<h1 align="center">omerta</h1>

<p align="center"><strong>Fail when a value of your env files, e.g. an API key in .env, is in git.</strong></p>

<p align="center">Your own values, not lookalikes · Staged, tracked or in the history · Never printed</p>

<p align="center">
<a href="https://pypi.org/project/omerta/"><img alt="PyPI version" src="https://img.shields.io/pypi/v/omerta?style=flat-square&labelColor=18181b&color=7c3aed"></a>
<a href="https://pypi.org/project/omerta/"><img alt="Python versions" src="https://img.shields.io/pypi/pyversions/omerta?style=flat-square&labelColor=18181b&color=7c3aed"></a>
<a href="https://github.com/shcherbak-ai/omerta/actions/workflows/ci.yml"><img alt="CI" src="https://img.shields.io/github/actions/workflow/status/shcherbak-ai/omerta/ci.yml?branch=main&style=flat-square&labelColor=18181b&label=CI"></a>
</p>

omerta reads the values of your env files (`.env`, `.env.local`, …) and fails when one of them is in git: in the staged changes, in a tracked file, or anywhere in the history. It matches your actual values, as they are written and as they are encoded in URLs, JSON and base64, so it catches secrets of any format: the key of a small provider, the name of a cloud resource, an organisation ID. What it reports is always one of your values, byte for byte, and it reports it by the name of its variable, never by the value.

It complements pattern scanners such as gitleaks, TruffleHog and detect-secrets, which find what looks like a key, not your keys. The name is the code of silence: your secrets never speak in git.

## Quick start

With [uv](https://docs.astral.sh/uv/), which brings its own Python when you have none:

```bash
uvx omerta check --all    # every tracked file
uvx omerta history        # every commit, tag and message
```

```text
src/settings.py:12: OPENAI_API_KEY (from .env)
tests/cassettes/chat.yaml:48: AZURE_ENDPOINT (from .env.local)
.env.production: tracked env file
```

Some values of an env file are no secrets, such as `DB_HOST=localhost` or `APP_ENV=production`, and they are in your code and docs on purpose. Tell omerta to leave those variables out, in `.omerta.toml` at the root of the repository (or `[tool.omerta]` of `pyproject.toml`):

```toml
ignore = ["DB_HOST", "APP_ENV"]
```

With pip: `pip install omerta`, then `omerta check --all`.

## Use it as a git hook

`omerta check` with no paths searches the staged changes, so any hook runner can call it before a commit.

### pre-commit

```yaml
# .pre-commit-config.yaml
repos:
  - repo: https://github.com/shcherbak-ai/omerta
    rev: v0.0.0
    hooks:
      - id: omerta
```

pre-commit installs the hook on the Python it runs on itself, which must be 3.11 or later. Install pre-commit with `uv tool install pre-commit` or `brew install pre-commit`, not from Ubuntu 22.04's apt (Python 3.10) or into macOS's system Python (3.9).

In a Python project, pin omerta in the lockfile instead (`uv add --dev omerta`) and run it from there:

```yaml
repos:
  - repo: local
    hooks:
      - id: omerta
        name: omerta
        entry: uv run omerta check
        language: system
        types: []
        types_or: [file, symlink]
```

### Husky, Lefthook, or a plain git hook

```bash
# .husky/pre-commit, or .git/hooks/pre-commit (made executable)
uvx omerta@0.0.0 check
```

```yaml
# lefthook.yml
pre-commit:
  commands:
    omerta:
      run: uvx omerta@0.0.0 check
```

## Use it in CI

In CI there is no `.env`, so there is no value to search for, but a committed env file is still found:

```yaml
# .github/workflows/omerta.yml
name: omerta
on: [push, pull_request]
permissions:
  contents: read
jobs:
  omerta:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
        with:
          fetch-depth: 0  # history needs all of it
      - uses: astral-sh/setup-uv@v10
      - run: uvx omerta@0.0.0 check --all
      - run: uvx omerta@0.0.0 history
```

## What it searches

| Command | Searches |
| --- | --- |
| `omerta check` | The staged changes, as git holds them (the index), not the working tree |
| `omerta check PATH ...` | The index content of the paths (a folder: everything tracked under it) |
| `omerta check --all` | Every tracked file |
| `omerta history` | Every file version reachable from a branch, a tag or `HEAD`, and the messages of the commits and annotated tags |

- **Env files** are `.env` and `.env.*` at the root of the repository, unless you name others. Only those that git does not track are read; templates (`*.example`, `*.sample`, `*.template`) never are.
- **A tracked env file is a leak in itself**, reported as `path: tracked env file`.
- **Each value is searched for** as written; a multi-line value also with CRLF line ends; a URL also without its trailing slash; and, from each of these, percent-encoded, as JSON string text, and as base64 at any position of a longer encoded text, in both alphabets. Binary files are searched too.
- **`history`** reports a value where it enters a file: at the commit that first writes it there, and again only if it leaves and comes back, or moves to another path.
- **Paths are searched too**: a value in a file or folder name is reported as such.
- **Values shorter than 8 characters are not searched for**: `true`, `5432` and `admin` are in too much unrelated text. Every private IPv4 address has 8 characters or more. If one of your secrets is shorter, lower `min-chars` and `ignore` the variables that then match ordinary text, such as `APP_ENV=staging`.

## Output and exit codes

One line per file, variable and env file, at the line of the first occurrence:

```text
path:line: VARIABLE (from .env)                 # check
<commit> path: VARIABLE (from .env)             # history
<commit> (message): VARIABLE (from .env)        # history: a commit or tag message
infra/<BUCKET>.json (path): BUCKET (from .env)  # a value in a file or folder name
path: tracked env file
```

A path or an env file's name that holds a value is shown with the variable's name in place of the value, as above.

`--json` writes JSON Lines instead, with the kind of form found (`plain`, `url`, `json` or `base64`):

```json
{"path": "src/settings.py", "line": 12, "variable": "OPENAI_API_KEY", "env_file": ".env", "form": "plain"}
```

The exit code is `0` when nothing is found, `1` when a value or a tracked env file is found, and `2` on an error, which wins over findings.

## Configuration

Options go after the command; `-C PATH` before it, as in git. Each has a setting of the same name in `.omerta.toml` or `[tool.omerta]` of `pyproject.toml` (one of the two), which the option overrides:

| Option | Setting | Effect |
| --- | --- | --- |
| `--env-file PATTERN` | `env-files = [...]` | Env file patterns, replacing `.env` and `.env.*`. `*` and `?` stay within a folder; `**` matches any number of folders |
| `--ignore NAME` | `ignore = [...]` | Variables not searched for; the option's names are added to the setting's |
| `--min-chars N` | `min-chars = N` | The shortest value searched for (default `8`) |
| `--no-encoded` | `encoded = false` | Search the values as written only, not their URL, JSON and base64 forms |
| `--json` | | Findings as JSON Lines |
| `-q`, `--quiet` | | No findings printed: the exit code says it |

A project that commits an env file on purpose, such as Symfony's `.env`, Vite's `.env.production` or an encrypted dotenvx `.env`, names the files that hold its secrets instead:

```toml
env-files = [".env.local", ".env.*.local"]
```

## Requirements

- **git 2.31** or later, found on `PATH`. In a partial clone (`--filter=blob:none`), git fetches what it reads unless told not to, so there omerta needs **git 2.45**, the first that honours `GIT_NO_LAZY_FETCH`.
- **Python 3.11** or later, which `uvx` brings along where it is missing.
- In a CI container whose user does not own the checkout, git refuses the repository ("dubious ownership"), and omerta reports git's error. Mark the checkout safe: `git config --global --add safe.directory "$PWD"`.

## Security

- omerta has no network code, and git never fetches for it: it runs git with `GIT_NO_LAZY_FETCH=1`, so a missing object is an error.
- No value leaves the process: not in the output, an error message, or git's command line. Where a printed path holds one, its variable's name stands in its place.
- git is run by its absolute path from `PATH`, never from a folder inside the repository it checks.

See [SECURITY.md](SECURITY.md) to report a vulnerability.

## License

[MIT](LICENSE)
