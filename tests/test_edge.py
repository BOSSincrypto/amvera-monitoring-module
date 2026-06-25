"""Edge-case тест: то, что ранее принимали на веру.

Проверяем:
  * пустая БД -> поведение всех 3 эндпоинтов
  * граница hours=720 (точно)
  * отрицательные/нулевые пороги (robustness)
  * idempotency configure_sqlite_for_monitoring (повторный вызов)
  * побочные эффекты импорта (нет I/O при import)
Запуск:  python tests/test_edge.py
"""
import asyncio
import os
import pathlib
import sys
import tempfile
import types

HERE = os.path.dirname(os.path.abspath(__file__))
MON = os.path.dirname(HERE)
_app = types.ModuleType("app")
_app.__path__ = [MON]
sys.modules["app"] = _app

import app.api.metrics_router as mr
from app.models.system_metric import Base
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

R: list[tuple[str, bool]] = []


def ck(name, cond, detail=""):
    R.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name + ((" :: " + detail) if (detail and not cond) else ""))


async def main():
    # ---------- 1. Пустая БД -> все эндпоинты ведут себя предсказуемо ----------
    tmp = pathlib.Path(tempfile.gettempdir()) / "mon_edge.db"
    if tmp.exists():
        tmp.unlink()
    eng = create_async_engine(f"sqlite+aiosqlite:///{tmp.as_posix()}")
    async with eng.begin() as c:
        await c.run_sync(Base.metadata.create_all)
    sf = async_sessionmaker(eng, expire_on_commit=False)

    app = FastAPI()
    app.include_router(mr.router)

    async def _gs():
        async with sf() as s:
            yield s

    app.dependency_overrides[mr.get_session] = _gs
    client = TestClient(app)

    r_latest = client.get("/api/metrics/latest")
    ck("edge: empty DB /latest -> 404", r_latest.status_code == 404, f"{r_latest.status_code}")
    r_hist = client.get("/api/metrics/history?hours=24")
    ck("edge: empty DB /history -> 200 []", r_hist.status_code == 200 and r_hist.json() == [],
       f"{r_hist.status_code} {r_hist.json()}")
    r_sum = client.get("/api/metrics/summary?hours=24")
    ck("edge: empty DB /summary -> 200 zeros",
       r_sum.status_code == 200 and r_sum.json().get("samples") == 0, f"{r_sum.json()}")

    # ---------- 2. Граница hours=720 (точно — должна пройти) ----------
    r720 = client.get("/api/metrics/history?hours=720")
    ck("edge: hours=720 boundary -> 200", r720.status_code == 200, f"{r720.status_code}")
    r721 = client.get("/api/metrics/history?hours=721")
    ck("edge: hours=721 -> 422", r721.status_code == 422, f"{r721.status_code}")

    # ---------- 3. Отрицательные/нулевые пороги (robustness) ----------
    # Не должно крашить при инициализации конфига; логика алерта определяется интегратором
    from app.services.monitor import MonitorConfig
    for k in [k for k in os.environ if k.startswith("MONITOR_")]:
        del os.environ[k]
    os.environ["MONITOR_CPU_THRESHOLD"] = "-5"
    os.environ["MONITOR_MEM_THRESHOLD"] = "0"
    os.environ["MONITOR_DISK_THRESHOLD"] = "150"  # > 100 — никогда не сработает
    cfg = MonitorConfig.from_env()
    ck("edge: negative cpu threshold accepted (no crash)", cfg.cpu_threshold == -5.0)
    ck("edge: zero mem threshold accepted (no crash)", cfg.mem_threshold == 0.0)
    ck("edge: over-100 disk threshold accepted (no crash)", cfg.disk_threshold == 150.0)

    # ---------- 4. Idempotency configure_sqlite_for_monitoring ----------
    from app.integration.scheduler_setup import configure_sqlite_for_monitoring
    from sqlalchemy import text
    eng_p = create_async_engine(f"sqlite+aiosqlite:///{(pathlib.Path(tempfile.gettempdir()) / 'mon_edge_p.db').as_posix()}")
    ck("edge: engine has no flag before configure",
       id(eng_p) not in __import__("app.integration.scheduler_setup", fromlist=["_configured_engines"])._configured_engines)
    configure_sqlite_for_monitoring(eng_p)
    configure_sqlite_for_monitoring(eng_p)  # повторный вызов
    configure_sqlite_for_monitoring(eng_p)  # третий раз — должен быть no-op
    from app.integration.scheduler_setup import _configured_engines
    ck("edge: idempotent — engine id tracked after configure",
       id(eng_p) in _configured_engines)
    # Подтверждаем, что WAL реально работает после нескольких вызовов
    async with eng_p.connect() as conn:
        mode = (await conn.execute(text("PRAGMA journal_mode"))).scalar()
        sync = (await conn.execute(text("PRAGMA synchronous"))).scalar()
        timeout = (await conn.execute(text("PRAGMA busy_timeout"))).scalar()
    await eng_p.dispose()
    ck("edge: WAL correct after 3x idempotent call", str(mode).lower() == "wal", f"mode={mode}")
    ck("edge: synchronous=NORMAL after 3x", int(sync) == 1, f"sync={sync}")
    ck("edge: busy_timeout=5000 after 3x", int(timeout) == 5000, f"timeout={timeout}")

    # ---------- 5. Побочные эффекты импорта: не должно создавать файлы/соединения ----------
    # Проверяем, что повторный import не вызывает ошибок и не создаёт ресурсов
    import importlib
    before = list(pathlib.Path(MON).rglob("*.db"))
    importlib.reload(mr)
    importlib.reload(mr)
    after = list(pathlib.Path(MON).rglob("*.db"))
    ck("edge: import has no file side-effects", len(before) == len(after),
       f"before={len(before)} after={len(after)}")

    # ---------- 6. configure_sqlite_for_monitoring на НЕ-SQLite движке ----------
    # Раньше это падало с SyntaxError на каждом соединении; теперь — безопасный no-op.
    from unittest.mock import MagicMock
    fake_engine = MagicMock()
    fake_engine.dialect.name = "postgresql"
    fake_engine.sync_engine = MagicMock()
    fake_id = id(fake_engine)
    ck("edge: non-sqlite engine not tracked before configure",
       fake_id not in _configured_engines)
    configure_sqlite_for_monitoring(fake_engine)  # не должно выбросить исключение
    ck("edge: non-sqlite engine skipped without crash",
       fake_id in _configured_engines)

    # ---------- SUMMARY ----------
    passed = sum(1 for _, c in R if c)
    print("\n==== EDGE SUMMARY ====")
    print(f"{passed}/{len(R)} passed")
    failed = [n for n, c in R if not c]
    if failed:
        print("FAILED:", failed)
    return passed == len(R)


def test_edge_main() -> None:
    """Pytest-обёртка над edge-case тестом."""
    assert asyncio.run(main())


if __name__ == "__main__":
    ok = asyncio.run(main())
    sys.exit(0 if ok else 1)
