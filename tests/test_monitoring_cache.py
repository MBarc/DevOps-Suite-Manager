"""Phase 20: persistent monitoring host-check cache (TTL + manual refresh)."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

import dosm.monitoring.routes as mon
from dosm.models import MonitoringMatch, MonitoringSource


class FakeAdapter:
    def __init__(self, source_id, source_name):
        self.source_id = source_id
        self.source_name = source_name
        self.calls = 0

    async def check_host(self, hostname):
        self.calls += 1
        return mon.HostCheckResult(
            source_id=self.source_id, source_name=self.source_name, tool="dynatrace",
            found=True, entity_id="e-1", entity_url="https://dt/e-1",
        )


def _make_source(session_factory, name="dt"):
    with session_factory() as s:
        src = MonitoringSource(name=name, tool="dynatrace", url="https://dt", enabled=True)
        s.add(src)
        s.commit()
        return src.id


def test_cache_hit_skips_api_and_persists(session_factory, test_config, monkeypatch):
    sid = _make_source(session_factory, "dt-hit")
    fake = FakeAdapter(sid, "dt-hit")
    monkeypatch.setattr(mon, "make_adapter", lambda source, t, t2: fake)

    async def run():
        # miss -> queries API, persists
        with session_factory() as db:
            src = db.get(MonitoringSource, sid)
            r1 = await mon._run_checks(db, "host-a", [src], None)
        assert r1[0].found and fake.calls == 1
        with session_factory() as db:
            row = db.execute(select(MonitoringMatch).where(
                MonitoringMatch.hostname == "host-a", MonitoringMatch.source_id == sid)).scalar_one()
            assert row.found and row.entity_url == "https://dt/e-1"
        # hit (fresh) -> served locally, API NOT called again
        with session_factory() as db:
            src = db.get(MonitoringSource, sid)
            r2 = await mon._run_checks(db, "host-a", [src], None)
        assert r2[0].found and fake.calls == 1
        # force -> re-queries
        with session_factory() as db:
            src = db.get(MonitoringSource, sid)
            await mon._run_checks(db, "host-a", [src], None, force=True)
        assert fake.calls == 2

    asyncio.run(run())


def test_stale_entry_requeries(session_factory, test_config, monkeypatch):
    sid = _make_source(session_factory, "dt-stale")
    fake = FakeAdapter(sid, "dt-stale")
    monkeypatch.setattr(mon, "make_adapter", lambda source, t, t2: fake)
    # seed a stale match (older than the TTL)
    with session_factory() as s:
        s.add(MonitoringMatch(
            hostname="host-b", source_id=sid, found=True, extra_json="{}",
            checked_at=datetime.now(UTC) - (mon._MATCH_TTL + timedelta(hours=1)),
        ))
        s.commit()

    async def run():
        with session_factory() as db:
            src = db.get(MonitoringSource, sid)
            await mon._run_checks(db, "host-b", [src], None)
        assert fake.calls == 1  # stale -> re-queried

    asyncio.run(run())


def test_match_fresh_helper():
    now = datetime.now(UTC)
    fresh = MonitoringMatch(hostname="h", source_id=1, checked_at=now)
    stale = MonitoringMatch(hostname="h", source_id=1, checked_at=now - (mon._MATCH_TTL + timedelta(minutes=1)))
    assert mon._match_fresh(fresh) is True
    assert mon._match_fresh(stale) is False
    assert mon._match_fresh(None) is False
