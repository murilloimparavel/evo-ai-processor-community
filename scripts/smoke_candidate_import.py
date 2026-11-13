"""Import the candidate application while making database access impossible."""
import os


# The application and its async database service need a URL during import, but
# this smoke test must never connect to a real database.
os.environ["POSTGRES_CONNECTION_STRING"] = (
    "postgresql://candidate:candidate@127.0.0.1:5432/candidate"
)

from sqlalchemy.engine import Engine

import asyncpg
import pg8000.dbapi
from src.config.database import Base


def fail_database_connect(*_args, **_kwargs):
    raise RuntimeError("Candidate import attempted a database connection")


# src.main normally creates tables at import time. Skip only that side effect;
# guard both sync/async driver entry points and SQLAlchemy as a second barrier.
Base.metadata.create_all = lambda *_args, **_kwargs: None
Engine.connect = fail_database_connect
pg8000.dbapi.connect = fail_database_connect
asyncpg.connect = fail_database_connect

import src.main

assert hasattr(src.main, "app")
print("Application import verified without database connections")
