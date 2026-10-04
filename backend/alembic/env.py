"""Alembic environment.

Autogenerate is a starting point only. Every generated migration is
reviewed by hand, because autogenerate does not produce the triggers,
partial indexes and check constraints that several documented invariants
require (DATABASE.md Section 6).
"""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

import app.models  # noqa: F401  (registers every mapper on Base.metadata)
from app.core.config import get_settings
from app.db.base import Base

# app.models is imported above so Base.metadata is fully populated for
# autogenerate. New model modules are added to that package's __init__.
target_metadata = Base.metadata

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Percent signs must be escaped: alembic.ini is parsed by ConfigParser,
# which treats "%" as interpolation syntax. A password containing "%"
# would otherwise raise at startup.
_url = get_settings().database_url.get_secret_value()
config.set_main_option("sqlalchemy.url", _url.replace("%", "%%"))


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        compare_type=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
