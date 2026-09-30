# Contributing to AMC

Thanks for helping! Bug reports, docs fixes and pull requests are welcome.

## Development setup

You need [uv](https://docs.astral.sh/uv/) and git; uv provides Python 3.11+.

```sh
git clone https://github.com/inflow-digital/amc
cd amc
uv sync --group dev
uv run amc --help
```

## Before opening a pull request

```sh
uv run ruff check src tests scripts
uv run pytest -q
python scripts/release_check.py      # no private addresses, home paths or secrets
```

CI runs the tests on Linux, macOS and Windows with Python 3.11 and 3.12, so:

- use `pathlib` and `os.pathsep`, not hard-coded `/` or `:`;
- if a test really can only run on POSIX, mark it with
  `pytest.mark.skipif(sys.platform == "win32", reason=...)` and say why.

Guidelines:

- Keep changes focused; one topic per pull request.
- Security-relevant behaviour (policy, confinement, auth, audit) needs tests, including the
  "denied" cases. Fail closed.
- Never log or print secrets beyond the one-time display of a newly created key.
- Use placeholders in docs and tests: `relay.example.com`, `alice`, `bob`, `ABCD-2345`.
- Update `docs/cli.md` when you change a command, and `THIRD_PARTY.md` when you add a
  dependency.

## Security issues

Please do not file public issues for vulnerabilities; see [SECURITY.md](SECURITY.md).

## License

By contributing you agree that your contributions are licensed under the MIT License
(see [LICENSE](LICENSE)).
