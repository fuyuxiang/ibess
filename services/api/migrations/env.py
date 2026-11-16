"""Alembic runs on the caller's transaction, including its schema search path."""
from alembic import context
from app.schema import load_metadata

connection = context.config.attributes.get("connection")
if connection is None:
    raise RuntimeError("Use python -m app.migrations upgrade; it owns the connection and migration lock")
context.configure(connection=connection, target_metadata=load_metadata(), compare_type=True)
with context.begin_transaction():
    context.run_migrations()
