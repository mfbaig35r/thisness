"""DuckDB 2.0 counts a result read with ``fetchone()`` as unfinished, and while one is
open on a connection it refuses ``register``, ``unregister`` and ``sql``: every later
read of the store would fail. Reads go through ``_first`` and ``_arrow`` instead."""

import re
from pathlib import Path

import thisness


def test_no_fetchone_in_the_package():
    package = Path(thisness.__file__).parent
    found = [
        f"{path.relative_to(package)}:{n}"
        for path in package.rglob("*.py")
        for n, line in enumerate(path.read_text().splitlines(), 1)
        if re.search(r"\.fetchone\(", line)
    ]
    assert found == []
