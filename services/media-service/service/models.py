"""Compatibility exports; evidence mapping is shared with the API and event consumer."""
from ibess_db.base import Base
from ibess_db.models import Evidence

__all__ = ["Base", "Evidence"]
