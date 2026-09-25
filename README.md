# NordicIntel Python template

A starting point for new NordicIntel Python repos: Python 3.13, uv, Ruff, pytest,
and Ubuntu CI. No runtime dependencies.

## Create a repository

1. Select **Use this template** on GitHub and create your repo under `nordicintel`.
2. Clone the new repo and install [uv](https://docs.astral.sh/uv/getting-started/installation/).
3. From its root, run:

```sh
uv run --no-project --python 3.13 scripts/setup_repo.py
```

Setup asks for the repository name, Python import name, description, whether to
enable PyPI publishing, and a license (`none` or `MIT`; add another license later
if needed). The owner is `nordicintel`, the import name is derived from the repo
name, and the initial version is `0.1.0`. No license is selected by default.

Setup replaces placeholders, installs the development environment, updates
`uv.lock`, and removes itself and its supporting files. Review and commit the
result. This is a one-time starting point; generated repos are independent.

For unattended setup, supply all choices:

```sh
uv run --no-project --python 3.13 scripts/setup_repo.py --repo example-project --module example_project --description "Example project." --no-publish --license none
```

Use `--publish` to include the release workflow.

## Included

- `src/<package>/`, `tests/`, and `scripts/`; one package import smoke test.
- `pyproject.toml`, `uv.lock`, and Python 3.13 configuration.
- `.github/workflows/checks.yml`: Ruff formatting/linting and pytest on pushes and PRs.
- A short project README with development and release commands and a prominent
  skeleton notice linking to this template. Remove the notice after the first
  real implementation commit, not during repository setup or preparation.

## Checks

```sh
uv sync --locked
uv run ruff format --check .
uv run ruff check .
uv run pytest
```

## Optional PyPI publishing

Set the distribution name in the generated repo's `pyproject.toml`. Commit the
version and lock file, then publish a GitHub Release with a matching tag such as
`v0.1.0`. The workflow verifies the version, runs checks, builds a wheel and source
distribution, and publishes both to PyPI.

**Publishing setup reminder:** In each generated repository, go to **Settings →
Secrets and variables → Actions** and create a repository secret named
`PYPI_TOKEN`. The PyPI token must permit uploads to the intended project.
No credentials belong in this template.

Publishing is absent from generated repos when disabled. Releases and uploads
are explicit actions; creating a repo or pushing a commit does not publish it.
