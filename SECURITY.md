# Security Policy

## Reporting a Vulnerability

If you discover a security vulnerability in omerta, please report it **privately** using [GitHub's private vulnerability reporting](https://github.com/shcherbak-ai/omerta/security/advisories/new). Please avoid opening public issues for security vulnerabilities. Confirmed vulnerabilities will be addressed in a patch release.

A value of an env file that omerta prints, writes to an error message, or puts on git's command line is a vulnerability, and so is a network connection that omerta or the git it runs makes.

## Security model

omerta reads the env files of a repository's working tree and searches git's objects for their values. It writes nothing, and runs no program but git.

- **No value leaves the process.** A finding names the file, the line and the variable, never the value; an error names the env file and the line, never its text; and git is never given a value on its command line, where other users of the machine could see it in the process list: PATH arguments are matched against the index by omerta itself. Everything omerta prints, a path, an env file's name or git's own error output, has each value replaced by the name of its variable.
- **No network.** omerta has no network code. git fetches a missing object of a partial clone when it is read, unless told not to: omerta runs git with `GIT_NO_LAZY_FETCH=1`, so a missing object is an error, and refuses a partial clone where git is older than 2.45, the first that honours it.
- **The git that runs is the one on `PATH`.** It is run by its absolute path, found by searching the folders of `PATH` in order and skipping relative entries, the current folder, and any folder inside the repository: on Windows, a program's own folder and the current folder are otherwise searched first, so a `git.exe` committed to the checked repository could run.
- **What omerta finds is not everything.** It finds the exact values of your env files, in the forms its README lists. A secret that is not in an env file, that is shorter than `min-chars`, that a variable of `ignore` holds, or that is otherwise transformed (compressed, encrypted, split) is not found. Use a pattern scanner beside it.
