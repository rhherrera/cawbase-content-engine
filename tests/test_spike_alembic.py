"""Foundational verification #2: several Alembic revisions inside ONE outer transaction.

Uses isolated, test-only revision chains (not product migrations).
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine

from cawbase_content_engine.errors import MigrationError, SchemaIncompatibleError
from cawbase_content_engine.migrations.runner import run_upgrade
from cawbase_content_engine.persistence.dialect import Backend
from cawbase_content_engine.persistence.sqlite_support import install_transaction_control

pytestmark = pytest.mark.spike


def make_chain(root: Path, revs: list[tuple[str, str | None, str]]) -> Path:
    (root / "versions").mkdir(parents=True)
    for rev, down, body in revs:
        (root / "versions" / f"{rev}.py").write_text(
            textwrap.dedent(
                f'''
                import sqlalchemy as sa
                from alembic import op
                revision = "{rev}"
                down_revision = {down!r}
                def upgrade():
                    {body}
                '''
            )
        )
    return root


def create(name: str) -> str:
    return f'op.create_table("{name}", sa.Column("id", sa.Integer, primary_key=True))'


FAIL = 'op.execute("SELECT * FROM table_that_does_not_exist")'


async def migrate(backend: str, url: str, chain: Path, *, timeout: float = 2.0):
    eng = create_async_engine(url)
    if backend == "sqlite":
        install_transaction_control(eng)
    try:
        async with eng.connect() as conn:
            if backend == "sqlite":
                conn = await conn.execution_options(sqlite_begin="IMMEDIATE")
            await conn.begin()
            try:
                report = await run_upgrade(conn, Backend(backend), lock_timeout=timeout, script_location=chain)
            except BaseException:
                await conn.rollback()
                raise
            await conn.commit()
            return report
    finally:
        await eng.dispose()


async def tables(url: str) -> set[str]:
    eng = create_async_engine(url)
    async with eng.connect() as c:
        names = await c.run_sync(lambda s: set(sa.inspect(s).get_table_names()))
        if "postgresql" in url:
            names |= await c.run_sync(
                lambda s: {f"cce.{n}" for n in sa.inspect(s).get_table_names("cawbase_content_engine")}
            )
    await eng.dispose()
    return names


async def test_several_revisions_apply_in_one_transaction(db, tmp_path):
    backend, url = db
    chain = make_chain(
        tmp_path / "ok", [("r1", None, create("t1")), ("r2", "r1", create("t2")), ("r3", "r2", create("t3"))]
    )
    report = await migrate(backend, url, chain)
    assert report.applied == ("r1", "r2", "r3")
    assert {"t1", "t2", "t3"} <= await tables(url)
    again = await migrate(backend, url, chain)
    assert again.already_current and again.applied == ()


async def test_intermediate_failure_rolls_back_everything(db, tmp_path):
    backend, url = db
    chain = make_chain(tmp_path / "bad", [("r1", None, create("t1")), ("r2", "r1", create("t2")), ("r3", "r2", FAIL)])
    with pytest.raises(MigrationError):
        await migrate(backend, url, chain)
    seen = await tables(url)
    assert not ({"t1", "t2"} & seen), "no revision may survive a failed sequence"
    assert not any("alembic_version" in n for n in seen), "history must roll back too"


async def test_foreign_objects_are_preserved(db, tmp_path):
    backend, url = db
    eng = create_async_engine(url)
    async with eng.begin() as c:
        await c.execute(sa.text("CREATE TABLE foreign_tbl (id INTEGER PRIMARY KEY)"))
        await c.execute(sa.text("INSERT INTO foreign_tbl VALUES (7)"))
        await c.execute(sa.text("CREATE VIEW foreign_view AS SELECT id FROM foreign_tbl"))
    chain = make_chain(tmp_path / "ok", [("r1", None, create("t1"))])
    await migrate(backend, url, chain)
    async with eng.connect() as c:
        assert (await c.execute(sa.text("SELECT id FROM foreign_view"))).scalar_one() == 7
    await eng.dispose()


async def test_foreign_objects_in_engine_namespace_are_refused(db, tmp_path):
    backend, url = db
    eng = create_async_engine(url)
    async with eng.begin() as c:
        if backend == "postgresql":
            await c.execute(sa.text('CREATE SCHEMA "cawbase_content_engine"'))
            await c.execute(sa.text('CREATE TABLE "cawbase_content_engine".stranger (id int)'))
        else:
            await c.execute(sa.text("CREATE TABLE cawbase_content_engine_stranger (id int)"))
    await eng.dispose()
    chain = make_chain(tmp_path / "ok", [("r1", None, create("t1"))])
    with pytest.raises(SchemaIncompatibleError):
        await migrate(backend, url, chain)


async def test_lock_wait_is_bounded_and_plan_is_computed_after_lock(db, tmp_path):
    """A second migrator waits for the lock; once it gets it, it sees the first one's result."""
    import asyncio

    backend, url = db
    chain = make_chain(tmp_path / "ok", [("r1", None, create("t1")), ("r2", "r1", create("t2"))])
    results = await asyncio.gather(migrate(backend, url, chain, timeout=20), migrate(backend, url, chain, timeout=20))
    applied = sorted(len(r.applied) for r in results)
    assert applied == [0, 2], "exactly one migrator applies the sequence; the other re-reads and finds nothing"
