"""Small SQLite store with checked, transactional migrations."""

import hashlib
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


class MigrationError(RuntimeError):
    """Database schema does not match the shipped migration history."""


@contextmanager
def connect(database: Path) -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(database, timeout=5)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 5000")
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def migrate(database: Path, migrations: Path | None = None) -> None:
    directory = migrations or Path(__file__).parent / "migrations"
    files = sorted(directory.glob("[0-9][0-9][0-9]_*.sql"))
    versions = [int(file.name.split("_", 1)[0]) for file in files]
    if versions != list(range(1, len(files) + 1)):
        raise MigrationError("迁移编号不连续")
    with connect(database) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations "
            "(version INTEGER PRIMARY KEY, checksum TEXT NOT NULL)"
        )
        applied = dict(connection.execute("SELECT version, checksum FROM schema_migrations"))
        if any(version not in versions for version in applied):
            raise MigrationError("数据库版本高于当前程序")
        if sorted(applied) != list(range(1, len(applied) + 1)):
            raise MigrationError("迁移记录不连续")
        for version, file in zip(versions, files, strict=True):
            raw = file.read_bytes()
            checksum = hashlib.sha256(raw).hexdigest()
            if version in applied:
                if applied[version] != checksum:
                    raise MigrationError("已执行迁移内容发生变化")
                continue
            # execute each complete statement without executescript's implicit COMMIT.
            statement = ""
            for line in raw.decode("utf-8").splitlines(keepends=True):
                statement += line
                if sqlite3.complete_statement(statement):
                    connection.execute(statement)
                    statement = ""
            if statement.strip():
                raise MigrationError("迁移 SQL 不完整")
            connection.execute(
                "INSERT INTO schema_migrations(version, checksum) VALUES (?, ?)",
                (version, checksum),
            )
