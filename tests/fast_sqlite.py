"""Shared test fixture: SQLite without fsync.

Every store in this project (onboard jobs, previews, queue, chains, roster) opens a fresh file connection
for each operation and commits, and each commit waits for the disk. In a test that cost most of the
runtime (the queue, onboarding and chain suites ran about 2.4 times slower). `PRAGMA synchronous=OFF`
removes only the wait for the disk: schema, locking, transactions and every code path are the same, and a
test store is a throwaway file, so durability is not wanted there.

Import this module from a test module that uses a store (`from tests import fast_sqlite  # noqa: F401`).
It patches `sqlite3.connect` once per process, and does nothing the second time.
"""

import sqlite3

_REAL_CONNECT = sqlite3.connect


def _connect(*args, **kwargs):
    connection = _REAL_CONNECT(*args, **kwargs)
    try:
        connection.execute("PRAGMA synchronous=OFF")
    except sqlite3.Error:
        pass                                    # a connection that refuses it still works as before
    return connection


def install() -> None:
    if sqlite3.connect is _REAL_CONNECT:
        sqlite3.connect = _connect


install()
