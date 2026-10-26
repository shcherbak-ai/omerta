# Changelog

All notable changes to this project will be documented in this file. Each version listed corresponds to a release published on [PyPI](https://pypi.org/project/omerta/).

The format is based on [Keep a Changelog](https://keepachangelog.com/), and this project adheres to [Semantic Versioning](https://semver.org/).

## [0.1.1] - 2026-10-26

### Fixed

- The error for an env file that cannot be read or parsed names it with the values of the other env files replaced by their variables' names, as findings do: an env file named after a value of another, such as `.env.<BUCKET>`, showed the value.

## [0.1.0] - 2026-10-25

### Added

- `omerta check`: fails when a value of the project's env files is in the index content of the staged changes, of the paths given, or with `--all` of every index entry.
- `omerta history`: fails when such a value entered any path in the history, or is in a blob under a tag, or in the message of a commit or a tag.
- The values are searched for as written, and as encoded in URLs, JSON and base64, in the files' content and in their paths; a tracked env file is reported as a leak.
- Options `-C`, `--min-chars`, `--env-file`, `--ignore`, `--encoded`/`--no-encoded`, `--json` and `--quiet`, and the settings `min-chars`, `env-files`, `ignore` and `encoded` in `[tool.omerta]` of `pyproject.toml` or in `.omerta.toml`.
- A pre-commit hook, `omerta`.
- Its two dependencies, python-dotenv and hyperscan, pinned to exact versions: a new release of either reaches users only through a release of omerta.

[0.1.1]: https://github.com/shcherbak-ai/omerta/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/shcherbak-ai/omerta/releases/tag/v0.1.0
