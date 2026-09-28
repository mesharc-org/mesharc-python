# Contributing

Thanks for helping with the MeshArc Python client. This repository is the only home of the
package that is published to PyPI as `mesharc`.

## Set up

Python 3.10 or newer.

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ".[dev,mcp]"
```

To try the client against an API running on your own machine, point it there:

```bash
export MESHARC_API_URL=http://localhost:8010
```

## Checks

These are what CI runs on every pull request, on Python 3.10 to 3.14:

```bash
ruff check .
mypy
python -m pytest -q
python scripts/check_release.py     # the version is the same in pyproject.toml and mesharc/__init__.py
```

## Pull requests

- Work on a branch and open a pull request against `main`. `main` only changes through pull
  requests whose checks pass, and every pull request is squash-merged.
- Keep a pull request to one change, with tests for it.
- Add a line under `## Unreleased` in `CHANGELOG.md` for anything a user of the client will
  notice. Internal changes (tests, CI) need no entry.
- Only add an option once the live API at mesharc.dev accepts it. The client must never offer
  something production refuses.
- Comments: docstrings on the public API, since they are the help users see in their editor.
  Inline comments only where the reason for the code is not obvious.

## Security

Please report vulnerabilities privately, as described in [SECURITY.md](SECURITY.md), not in an
issue.

## Releasing (maintainers)

Releases are published from a tag, by `.github/workflows/release.yml`, through PyPI trusted
publishing. No token is stored anywhere.

1. In one pull request, set the new version in `pyproject.toml` and `mesharc/__init__.py`, and
   rename `## Unreleased` in `CHANGELOG.md` to that version, leaving a fresh empty
   `## Unreleased` above it.
2. After it is merged, tag the merge commit and push the tag:

   ```bash
   git tag v0.2.0
   git push origin v0.2.0
   ```

3. The workflow checks that the tag, the version and the changelog agree, tests and builds the
   package, then waits for an owner to approve the `pypi` environment. Once approved it
   publishes to PyPI (with attestations) and creates the GitHub Release from the changelog
   section.

Versioning follows [SemVer](https://semver.org). While the version is 0.x, a minor release may
contain breaking changes, and the changelog says so. A Python version is supported until six
months after its upstream end of life; dropping one is a minor release with a changelog entry.

To rehearse a release without publishing, run the Release workflow by hand (Actions → Release →
Run workflow). It checks, tests, builds and inspects the package, and publishes nothing.
