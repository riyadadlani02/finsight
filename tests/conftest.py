import os

import pytest
from sqlalchemy import create_engine

from core import db


@pytest.fixture
def engine():
    """Fresh schema per test: in-memory SQLite, or TEST_DATABASE_URL (e.g. Postgres)."""
    eng = create_engine(os.environ.get("TEST_DATABASE_URL", "sqlite://"))
    db.metadata.drop_all(eng)
    db.metadata.create_all(eng)
    yield eng
    db.metadata.drop_all(eng)
