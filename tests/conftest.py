from __future__ import annotations

import sqlite3
from typing import Iterator

import pytest

from onlybirds.db import init_schema


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    init_schema(c)
    yield c
    c.close()
