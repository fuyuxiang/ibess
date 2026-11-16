"""Use the same explicit metadata registry as migrations, independent of router import order."""
from app.schema import load_metadata


def pytest_sessionstart(session):
    load_metadata()
