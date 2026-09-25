> **Python project skeleton — no application code implemented yet.**
> This repository was created from the
> [NordicIntel Python template](https://github.com/nordicintel/python-template).
> It contains project scaffolding and a setup smoke test, not a working implementation.
> **Remove this notice after the first commit implementing real project functionality;
> repository setup and preparation commits do not count.**

# wayback-verify

Async Wayback archiving and file verification with SQLite history.

## Development

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then:

```sh
uv sync --locked
uv run ruff format .
uv run ruff check .
uv run pytest
```

Code lives in `src/wayback_verify/`, tests in `tests/`, and utilities in `scripts/`.
Python 3.13 is used locally and in Ubuntu CI. Commit `uv.lock` when dependencies
change. CI checks formatting with `uv run ruff format --check .`.

## Releases

PyPI publishing is not configured. To build local distributions, run `uv build`.

## License

No license has been selected. Add one when appropriate.

## Implementation plan

See [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md) for the package scope, API,
implementation order, and verification requirements. The functionality described
there is planned; this repository currently contains the configured starter.
