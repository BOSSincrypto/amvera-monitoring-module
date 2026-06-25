"""Юнит- и интеграционные тесты, дополняющие скриптовые дебаг-тесты."""
from __future__ import annotations

import os

import app.api.metrics_router as mr
import app.services.monitor as monitor_module
import pytest
from app.integration.scheduler_setup import (
    build_monitor_service,
    configure_sqlite_for_monitoring,
    register_monitor_jobs,
)
from app.models.system_metric import Base, SystemMetric
from app.services.monitor import MonitorConfig, MonitorService
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str, str | None]] = []

    async def send_message(self, chat_id: int, text: str, parse_mode: str | None = None) -> None:
        self.sent.append((chat_id, text, parse_mode))


@pytest.fixture
async def session_factory(tmp_path):
    db = tmp_path / "unit.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db.as_posix()}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sf = async_sessionmaker(engine, expire_on_commit=False)
    yield sf
    await engine.dispose()


@pytest.fixture
def client(session_factory):
    app = FastAPI()
    app.include_router(mr.router)

    async def _get_session():
        async with session_factory() as s:
            yield s

    app.dependency_overrides[mr.get_session] = _get_session
    return TestClient(app)


# ---------------------------------------------------------------------------
# FastAPI router
# ---------------------------------------------------------------------------

def test_latest_empty_db_returns_404(client):
    assert client.get("/api/metrics/latest").status_code == 404


def test_history_empty_db_returns_empty_list(client):
    r = client.get("/api/metrics/history?hours=24")
    assert r.status_code == 200
    assert r.json() == []


def test_summary_empty_db_returns_zero_samples(client):
    r = client.get("/api/metrics/summary?hours=24")
    assert r.status_code == 200
    assert r.json()["samples"] == 0


def test_token_whitespace_only_treated_as_unset(client, monkeypatch):
    monkeypatch.setenv("MONITOR_API_TOKEN", "   ")
    # Перезагружаем лениво считываемый токен роутера
    r = client.get("/api/metrics/latest")
    assert r.status_code == 404  # не 401 — токен не задан


def test_token_matches_exactly(client, monkeypatch):
    monkeypatch.setenv("MONITOR_API_TOKEN", "tok-exact")
    r = client.get("/api/metrics/latest", headers={"X-Monitor-Token": "tok-exact"})
    assert r.status_code == 404  # токен совпал, данных нет

    r = client.get("/api/metrics/latest", headers={"X-Monitor-Token": "tok-wrong"})
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# MonitorService functional
# ---------------------------------------------------------------------------

async def test_collect_load_disabled(session_factory, monkeypatch):
    monkeypatch.setenv("MONITOR_COLLECT_LOAD", "false")
    cfg = MonitorConfig.from_env()
    svc = MonitorService(bot=None, session_factory=session_factory, config=cfg)
    data = svc._collect_snapshot()
    assert data["load_avg_1"] == 0.0
    assert data["load_avg_5"] == 0.0
    assert data["load_avg_15"] == 0.0


async def test_collect_snapshot_exception_is_caught(session_factory, monkeypatch):
    svc = MonitorService(bot=None, session_factory=session_factory)

    def _boom(**_kwargs):
        raise RuntimeError("psutil failed")

    monkeypatch.setattr(monitor_module.psutil, "cpu_percent", _boom)
    # не должно вылетать наружу
    await svc.collect_and_store()


async def test_alert_exception_is_caught(session_factory, monkeypatch):
    class BadBot:
        async def send_message(self, chat_id, text, parse_mode=None):
            raise RuntimeError("send failed")

    monkeypatch.setenv("MONITOR_ADMIN_CHAT_ID", "123")
    monkeypatch.setenv("MONITOR_CPU_THRESHOLD", "0")
    cfg = MonitorConfig.from_env()
    svc = MonitorService(bot=BadBot(), session_factory=session_factory, config=cfg)
    await svc.collect_and_store()  # не валится


async def test_cleanup_exception_returns_zero(session_factory):
    class BadSF:
        def __call__(self):
            raise RuntimeError("no db")

    svc = MonitorService(bot=None, session_factory=BadSF())
    assert await svc.cleanup_old() == 0


async def test_env_int_fallback():
    os.environ["MONITOR_INTERVAL_SEC"] = "12.0"
    try:
        cfg = MonitorConfig.from_env()
        assert cfg.interval_sec == 12
    finally:
        del os.environ["MONITOR_INTERVAL_SEC"]


async def test_env_bool_variants():
    from app.services.monitor import _env_bool

    for val in ("1", "true", "True", "yes", "on"):
        os.environ["_TEST_BOOL"] = val
        assert _env_bool("_TEST_BOOL", False) is True
    for val in ("0", "false", "False", "no", "off", "maybe"):
        os.environ["_TEST_BOOL"] = val
        assert _env_bool("_TEST_BOOL", True) is False
    # Пустое значение возвращает дефолт
    os.environ["_TEST_BOOL"] = ""
    assert _env_bool("_TEST_BOOL", True) is True
    assert _env_bool("_TEST_BOOL", False) is False
    del os.environ["_TEST_BOOL"]


# ---------------------------------------------------------------------------
# Integration: scheduler_setup
# ---------------------------------------------------------------------------

def test_register_monitor_jobs_creates_collect_and_cleanup(session_factory):
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    scheduler = AsyncIOScheduler()
    bot = FakeBot()
    svc = register_monitor_jobs(scheduler, bot=bot, session_factory=session_factory)
    try:
        jobs = scheduler.get_jobs()
        ids = {job.id for job in jobs}
        assert "monitor:collect" in ids
        assert "monitor:cleanup" in ids

        collect_job = next(job for job in jobs if job.id == "monitor:collect")
        # По умолчанию интервал 60 секунд
        assert collect_job.trigger.interval.total_seconds() == svc.config.interval_sec == 60
    finally:
        if scheduler.running:
            scheduler.shutdown(wait=False)


def test_register_monitor_jobs_respects_interval(session_factory, monkeypatch):
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    monkeypatch.setenv("MONITOR_INTERVAL_SEC", "120")
    scheduler = AsyncIOScheduler()
    _ = register_monitor_jobs(scheduler, bot=None, session_factory=session_factory)
    try:
        collect_job = next(job for job in scheduler.get_jobs() if job.id == "monitor:collect")
        assert collect_job.trigger.interval.total_seconds() == 120
    finally:
        if scheduler.running:
            scheduler.shutdown(wait=False)
    monkeypatch.delenv("MONITOR_INTERVAL_SEC", raising=False)


def test_register_monitor_jobs_enforces_min_interval(session_factory, monkeypatch):
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    monkeypatch.setenv("MONITOR_INTERVAL_SEC", "5")
    scheduler = AsyncIOScheduler()
    _ = register_monitor_jobs(scheduler, bot=None, session_factory=session_factory)
    try:
        collect_job = next(job for job in scheduler.get_jobs() if job.id == "monitor:collect")
        assert collect_job.trigger.interval.total_seconds() == 15
    finally:
        if scheduler.running:
            scheduler.shutdown(wait=False)
    monkeypatch.delenv("MONITOR_INTERVAL_SEC", raising=False)


def test_build_monitor_service_uses_env(session_factory, monkeypatch):
    monkeypatch.setenv("MONITOR_SERVER_NAME", "unittest-server")
    svc = build_monitor_service(bot=None, session_factory=session_factory)
    assert svc.config.server_name == "unittest-server"
    monkeypatch.delenv("MONITOR_SERVER_NAME", raising=False)


async def test_configure_sqlite_for_monitoring_idempotent():
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as db:
        db.close()
        db_path = db.name
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    configure_sqlite_for_monitoring(engine)
    configure_sqlite_for_monitoring(engine)  # не добавляет дублирующих listener'ов
    from app.integration.scheduler_setup import _configured_engines

    assert id(engine) in _configured_engines
    await engine.dispose()
    os.unlink(db_path)


# ---------------------------------------------------------------------------
# Admin view (smoke)
# ---------------------------------------------------------------------------

def test_system_metric_admin_attributes():
    from app.admin.system_metric_admin import SystemMetricAdmin

    assert SystemMetricAdmin.name == "Метрики сервера"
    assert SystemMetricAdmin.can_create is False
    assert SystemMetricAdmin.can_edit is False
    assert SystemMetricAdmin.can_delete is True
    assert SystemMetric.timestamp in SystemMetricAdmin.column_list
