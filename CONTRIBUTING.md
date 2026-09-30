# Contributing

Thanks for helping. Keep changes small and focused, one concern per pull
request.

## Set up and check

You need Python 3.14 and, for the integration suites, Docker (Docker Desktop
or Colima with at least 4 GB of memory).

```sh
make check          # lint, format check, mypy, unit tests with coverage, docs check
make test-all       # also starts the test stack and runs the broker and gateway suites
make test-multi     # two replicas behind nginx (restarts the stack)
make stack-down     # stop the stack
```

Run `make help` for every target. Check links with
`docker run --rm -v "$PWD:/input:ro" -w /input lycheeverse/lychee:0.24.2 --config lychee.toml README.md 'docs/**/*.md'`.

## Rules that reviews enforce

These are the project's invariants (see [CLAUDE.md](CLAUDE.md) and
[docs/design.md](docs/design.md)); tests guard each of them:

- The broker is a custodian, not an issuer: exactly seven routes, no token
  minting, no JWKS (`tests/unit/test_routes.py`).
- The wire contract is frozen: resolve status codes, response fields,
  problem `title` slugs, and audit event names
  (`tests/integration/test_wire_compat.py`). Additions only.
- No token material in logs, errors, or audit events.
- The KV-v2 compare-and-swap is the correctness backstop; never write a
  token pair after losing it.
- Fail closed, and distinguishably (503 `vault-unavailable` vs
  `coordination-unavailable`).

Registry changes are reviewed changes: `scope_ceiling` is a security
boundary.

## Conventions

- Commits: [Conventional Commits](https://www.conventionalcommits.org/)
  (`feat:`, `fix:`, `docs:`, `test:`, `refactor:`, `ci:`, `build:`, `chore:`),
  first line under 72 characters.
- Formatting and lint: `make format`.
- Dependencies: edit `pyproject.toml`, then `make lock` (needs
  [uv](https://docs.astral.sh/uv/)) and commit all three `requirements*.lock`
  files.
- Docs: the Markdown under `docs/` is the source. After changing it, run
  `.venv/bin/python tools/build-pages.py` and commit `docs/index.html`
  ([Maintaining the docs](docs/maintaining.md)).
- Add an entry to [CHANGELOG.md](CHANGELOG.md) for user-visible changes.

Security issues: see [SECURITY.md](SECURITY.md), not the issue tracker.
