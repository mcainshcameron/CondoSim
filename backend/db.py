"""Postgres connection pool for Condominio.

Holds an asyncpg pool backed by DATABASE_URL (Supabase). Runs schema
migrations from db/migrations/*.sql on startup (idempotent — each migration
uses CREATE TABLE IF NOT EXISTS etc.).

`statement_cache_size=0` is set defensively so the pool works regardless
of whether DATABASE_URL points at a direct Postgres connection, the
Supabase session pooler, or the transaction pooler (which disables
session-level prepared statements).
"""
from __future__ import annotations

import os
from pathlib import Path

import asyncpg

from .logging_utils import log, log_error


_pool: asyncpg.Pool | None = None


def has_database() -> bool:
    """Is a Postgres backend configured?

    When DATABASE_URL is unset the app runs against an in-process store
    (`storage.py` / `memory.py` both fall back). That makes local dev and
    the offline test suite runnable with zero infrastructure — the previous
    hard requirement meant you could not even start the server, let alone
    run a test, without a live Supabase URL.

    Production always sets DATABASE_URL, so this never engages on Heroku.
    """
    return bool(os.getenv("DATABASE_URL", "").strip())


def database_url() -> str:
    url = os.getenv("DATABASE_URL", "").strip()
    if not url:
        raise RuntimeError(
            "DATABASE_URL is not set. "
            "Set it in .env for local dev or in Heroku config vars for prod."
        )
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    return url


async def init_pool() -> None:
    global _pool
    if _pool is not None:
        return
    if not has_database():
        log("db", "DATABASE_URL unset — using in-memory store (state is lost on restart)")
        return
    _pool = await asyncpg.create_pool(
        database_url(),
        min_size=1,
        max_size=10,
        command_timeout=30.0,
        statement_cache_size=0,
        max_inactive_connection_lifetime=60.0,
    )
    log("db", "pool initialized")
    await run_migrations()


async def close_pool() -> None:
    global _pool
    if _pool is None:
        return
    await _pool.close()
    _pool = None
    log("db", "pool closed")


def pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("DB pool not initialized — init_pool() must run at startup")
    return _pool


async def run_migrations() -> None:
    migrations_dir = Path(__file__).resolve().parent.parent / "db" / "migrations"
    if not migrations_dir.exists():
        log("db", f"no migrations dir at {migrations_dir}, skipping")
        return
    paths = sorted(migrations_dir.glob("*.sql"))
    if not paths:
        log("db", "no migrations found, skipping")
        return
    async with pool().acquire() as conn:
        for p in paths:
            sql = p.read_text(encoding="utf-8")
            try:
                await conn.execute(sql)
                log("db", f"applied migration {p.name}")
            except Exception as exc:
                log_error("db", f"migration {p.name} failed: {exc!r}")
                raise
