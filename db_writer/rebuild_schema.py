"""
Drop and recreate the whole schema.

DESTRUCTIVE — every user, stream, flight and alert is deleted. This exists because
the streams table and the new flights columns landed while the database held only
test data; once there is anything real in it, this script stops being the right
tool and the change needs a migration instead.

Usage (from the db_writer directory, with the DB_* env vars set):

    python rebuild_schema.py --drop
    python rebuild_schema.py --drop --seed-user me@example.com --seed-password secret

Without --drop it only creates missing tables, which is safe but will NOT add the
new columns to a flights table that already exists — SQLAlchemy's create_all does
not alter existing tables.
"""

import argparse
import logging
import os
import sys

from sqlalchemy import create_engine

from db_manager import Base, UserDirectory

logging.basicConfig(level=logging.INFO, format="%(levelname)s - %(message)s")
logger = logging.getLogger("rebuild_schema")


def database_url() -> str:
    service = os.environ["DB_SERVICE"].lower()
    host = os.environ["DB_HOST"]
    port = int(os.environ.get("DB_PORT", 5432))
    name = os.environ["DB_NAME"]
    user = os.environ["DB_WORKER_NAME"]
    password = os.environ["DB_WORKER_PASSWORD"]

    if service == "postgresql":
        return f"postgresql://{user}:{password}@{host}:{port}/{name}"
    if service == "mysql":
        return f"mysql+pymysql://{user}:{password}@{host}:{port}/{name}"
    raise ValueError(f"Unsupported DB_SERVICE '{service}'. Use 'postgresql' or 'mysql'.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--drop", action="store_true",
                        help="DROP every table first. Destroys all data.")
    parser.add_argument("--seed-user", help="Create this user after building the schema")
    parser.add_argument("--seed-password", help="Password for --seed-user")
    parser.add_argument("--seed-stream", default="test stream",
                        help="Label for a stream slot added to the seeded user")
    args = parser.parse_args()

    if args.seed_user and not args.seed_password:
        parser.error("--seed-user requires --seed-password")

    url = database_url()
    # Log without the password — this output tends to end up in terminal scrollback.
    logger.info(f"Target: {url.split('@')[-1]}")

    engine = create_engine(url, pool_pre_ping=True, connect_args={'connect_timeout': 5})

    if args.drop:
        confirm = input("This DELETES all users, streams, flights and alerts. Type 'yes': ")
        if confirm.strip().lower() != "yes":
            logger.info("Aborted — nothing was changed.")
            return 1
        logger.info("Dropping all tables...")
        Base.metadata.drop_all(engine)

    logger.info("Creating tables...")
    Base.metadata.create_all(engine)
    logger.info(f"Tables present: {', '.join(sorted(Base.metadata.tables))}")

    if args.seed_user:
        # Through UserDirectory rather than building the rows directly, so seeding
        # and registration are the same code path. A seeded account that the portal
        # would have refused to create — a too-short password, an address stored in
        # a casing that then fails to authenticate — is a test fixture that does not
        # represent a real user, and the bug it hides shows up in production only.
        directory = UserDirectory.__new__(UserDirectory)
        directory._engine = engine

        try:
            user = directory.create_user(args.seed_user, args.seed_password)
        except ValueError as e:
            logger.error(f"Cannot seed user: {e}")
            return 1

        stream = directory.create_stream(user["user_id"], args.seed_stream)

        logger.info(f"Seeded user_id={user['user_id']} ({user['email']})")
        logger.info(f"Seeded stream_id={stream['stream_id']} ({args.seed_stream})")
        print()
        print(f"  stream key : {stream['stream_key']}")
        print(f"  ingest URL : rtmp://<host>:1935/in/{stream['stream_key']}")
        print()

    logger.info("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
