"""Check that the version is stated once, and print a release's notes.

    python scripts/check_release.py              the version in pyproject.toml and __version__ agree
    python scripts/check_release.py v0.2.0       ...and the tag names that version, and CHANGELOG.md has its section
    python scripts/check_release.py v0.2.0 --notes    print that section, for the GitHub Release
"""

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent


def fail(message):
    print(f"error: {message}", file=sys.stderr)
    sys.exit(1)


def read(path):
    return (ROOT / path).read_text(encoding="utf-8")


def project_version():
    found = re.search(r'^version = "([^"]+)"', read("pyproject.toml"), re.M)
    if not found:
        fail("pyproject.toml has no version")
    return found.group(1)


def module_version():
    found = re.search(r'^__version__ = "([^"]+)"', read("mesharc/__init__.py"), re.M)
    if not found:
        fail("mesharc/__init__.py has no __version__")
    return found.group(1)


def changelog_section(version):
    lines = read("CHANGELOG.md").splitlines()
    heading = re.compile(rf"^## \[?{re.escape(version)}\]?(\s|$)")
    start = next((i for i, line in enumerate(lines) if heading.match(line)), None)
    if start is None:
        return None
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    return "\n".join(lines[start + 1:end]).strip()


def main(argv):
    version = project_version()
    if module_version() != version:
        fail(f"pyproject.toml says {version}, mesharc/__init__.py says {module_version()}")
    tag = argv[0] if argv and not argv[0].startswith("--") else ""
    if not tag:
        print(f"version {version}")
        return
    if tag != f"v{version}":
        fail(f"tag {tag} does not match version {version}")
    notes = changelog_section(version)
    if not notes:
        fail(f"CHANGELOG.md has no section for {version}")
    if "--notes" in argv:
        print(notes)
    else:
        print(f"release {tag} checks out")


if __name__ == "__main__":
    main(sys.argv[1:])
