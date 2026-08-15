#!/usr/bin/env python3
"""Print CHANGELOG.md's section for one version, for use as release notes.

    python scripts/changelog_section.py 0.4.0

Exits 1 with a message on stderr if there is no `## [X.Y.Z]` heading for that
version, or the section under it is empty. A release published with the wrong
notes is worse than one that fails to publish.
"""

import re
import sys
from pathlib import Path

CHANGELOG = Path(__file__).resolve().parents[1] / "CHANGELOG.md"


def section(text, version):
    """The body under `## [version] ...`, up to the next `## ` heading."""
    pattern = re.compile(
        r"^## \[" + re.escape(version) + r"\][^\n]*\n(.*?)(?=^## |\Z)", re.S | re.M)
    match = pattern.search(text)
    return match.group(1).strip() if match else None


def main():
    if len(sys.argv) != 2:
        sys.exit("usage: changelog_section.py X.Y.Z")
    version = sys.argv[1].lstrip("v")
    body = section(CHANGELOG.read_text(encoding="utf-8"), version)
    if body is None:
        sys.exit(f"CHANGELOG.md has no '## [{version}]' section — add one before tagging.")
    if not body:
        sys.exit(f"CHANGELOG.md's '## [{version}]' section is empty — write the notes first.")
    print(body)


if __name__ == "__main__":
    main()
