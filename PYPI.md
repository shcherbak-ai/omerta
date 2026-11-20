# omerta

Catch `.env` secrets leaking into git: a pre-commit hook and CLI that fails when an API key or any value of your env files is staged, tracked or anywhere in the history.

```bash
uvx omerta check --all    # every tracked file
uvx omerta history        # every commit, tag and message
uvx omerta docker         # what your Dockerfiles' builds would copy
```

- Matches your actual values, as written and as encoded in URLs, JSON and base64, so it catches secrets of any format; it complements pattern scanners, which find what looks like a key.
- Reports the file, the line and the variable's name, never the value; a tracked env file is a leak in itself.
- Fails when `.dockerignore`, which Docker reads instead of `.gitignore`, leaves an env file in a Dockerfile's build context, matched as Docker's BuildKit matches it.
- Runs entirely offline.

As pre-commit hooks:

```yaml
repos:
  - repo: https://github.com/shcherbak-ai/omerta
    rev: v0.1.3
    hooks:
      - id: omerta
      - id: omerta-docker
```

With Husky, Lefthook or a plain git hook: `uvx omerta@0.1.3 check`.

See the [repository](https://github.com/shcherbak-ai/omerta#readme) for the options, CI and configuration.

Licensed under [MIT](https://github.com/shcherbak-ai/omerta/blob/main/LICENSE).
