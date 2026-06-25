"""Дополнительные security-тесты для HTTP API и алертов."""
from __future__ import annotations

import app.api.metrics_router as mr
import pytest
from app.models.system_metric import Base
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


@pytest.fixture
async def session_factory(tmp_path):
    db = tmp_path / "sec.db"
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


def test_token_compare_uses_secrets_compare_digest(client, monkeypatch):
    """Убедиться, что сравнение токена идёт через secrets.compare_digest."""
    called = []

    def fake_compare(a: bytes, b: bytes) -> bool:
        called.append((a, b))
        return False

    monkeypatch.setenv("MONITOR_API_TOKEN", "secret")
    monkeypatch.setattr(mr.secrets, "compare_digest", fake_compare)

    r = client.get("/api/metrics/latest", headers={"X-Monitor-Token": "wrong"})
    assert r.status_code == 401
    assert len(called) == 1
    assert called[0] == (b"wrong", b"secret")


def test_401_does_not_leak_internals(client, monkeypatch):
    monkeypatch.setenv("MONITOR_API_TOKEN", "secret")
    r = client.get("/api/metrics/latest")
    assert r.status_code == 401
    body = r.text.lower()
    assert "secret" not in body
    assert "traceback" not in body
    assert "stack" not in body


def test_html_escape_in_alert_text(session_factory, monkeypatch):
    from app.models.system_metric import SystemMetric
    from app.services.monitor import MonitorConfig, MonitorService
    from app.services.monitor import _utcnow as monitor_utcnow

    class CapturingBot:
        def __init__(self):
            self.sent: list[tuple[int, str, str | None]] = []

        async def send_message(self, chat_id, text, parse_mode=None):
            self.sent.append((chat_id, text, parse_mode))

    monkeypatch.setenv("MONITOR_ADMIN_CHAT_ID", "777")
    monkeypatch.setenv("MONITOR_CPU_THRESHOLD", "0")
    monkeypatch.setenv("MONITOR_SERVER_NAME", "<script>alert(1)</script>")
    cfg = MonitorConfig.from_env()
    bot = CapturingBot()
    svc = MonitorService(bot=bot, session_factory=session_factory, config=cfg)

    metric = SystemMetric(
        timestamp=monitor_utcnow(),
        cpu_percent=100.0,
        mem_total_mb=1000.0,
        mem_used_mb=500.0,
        mem_percent=50.0,
        disk_total_gb=100.0,
        disk_used_gb=50.0,
        disk_percent=50.0,
        net_bytes_sent=0,
        net_bytes_recv=0,
        load_avg_1=0.0,
        load_avg_5=0.0,
        load_avg_15=0.0,
    )

    import asyncio

    asyncio.run(svc._check_and_alert(metric))
    assert len(bot.sent) == 1
    text = bot.sent[0][1]
    assert "<script>" not in text
    assert "&lt;script&gt;" in text
    assert bot.sent[0][2] == "HTML"


def test_history_does_not_accept_out_of_range_hours(client):
    # Отрицательное значение
    assert client.get("/api/metrics/history?hours=-1").status_code == 422
    # Больше максимума
    assert client.get("/api/metrics/history?hours=721").status_code == 422
