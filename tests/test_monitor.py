"""Дебаг-тест модуля мониторинга.

Без aiogram -> FakeBot. Без apscheduler -> прямой вызов collect_and_store().
Без реальной БД -> временный SQLite-файл.
Запуск:  python tests/test_monitor.py
"""
import asyncio
import os
import pathlib
import sys
import tempfile
import types
from datetime import timedelta

# --- bootstrap: делаем monitoring-module доступным как пакет 'app' ---
HERE = os.path.dirname(os.path.abspath(__file__))
MON = os.path.dirname(HERE)  # корень monitoring-module
_app = types.ModuleType("app")
_app.__path__ = [MON]
sys.modules["app"] = _app

from app.models.system_metric import Base, SystemMetric
from app.services.monitor import MonitorConfig, MonitorService, _utcnow
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

R: list[tuple[str, bool, str]] = []


def ck(name, cond, detail=""):
    R.append((name, bool(cond), detail))
    print(("PASS " if cond else "FAIL ") + name + ((" :: " + detail) if (detail and not cond) else ""))


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, parse_mode=None):
        self.sent.append((chat_id, text, parse_mode))


def env_clear():
    for k in [k for k in os.environ if k.startswith("MONITOR_")]:
        del os.environ[k]


def env_set(**kw):
    for k, v in kw.items():
        os.environ["MONITOR_" + k.upper()] = str(v)


async def main():
    # ---------- БД ----------
    tmp = pathlib.Path(tempfile.gettempdir()) / "mon_dbg.db"
    if tmp.exists():
        tmp.unlink()
    eng = create_async_engine(f"sqlite+aiosqlite:///{tmp.as_posix()}")
    async with eng.begin() as c:
        await c.run_sync(Base.metadata.create_all)
    sf = async_sessionmaker(eng, expire_on_commit=False)

    # ---------- Config: значения по умолчанию ----------
    env_clear()
    cfg = MonitorConfig.from_env()
    ck("cfg: admin_chat_id None default", cfg.admin_chat_id is None)
    ck("cfg: interval 60", cfg.interval_sec == 60)
    ck("cfg: retention 30", cfg.retention_days == 30)
    ck("cfg: cpu_thr 90", cfg.cpu_threshold == 90.0)
    ck("cfg: mem_thr 85", cfg.mem_threshold == 85.0)
    ck("cfg: disk_path /data", cfg.disk_path == "/data")
    ck("cfg: cooldown 300", cfg.cooldown_sec == 300.0)

    # ---------- Config: валидные значения ----------
    env_clear()
    env_set(admin_chat_id="4242", interval_sec="15", cpu_threshold="50", disk_path=".")
    cfg2 = MonitorConfig.from_env()
    ck("cfg: chat_id 4242", cfg2.admin_chat_id == 4242)
    ck("cfg: interval 15", cfg2.interval_sec == 15)
    ck("cfg: cpu_thr 50", cfg2.cpu_threshold == 50.0)

    # ---------- Config: НЕвалидные значения -> дефолты ----------
    env_clear()
    env_set(admin_chat_id="not_int", interval_sec="abc", cpu_threshold="x", mem_threshold="")
    cfg3 = MonitorConfig.from_env()
    ck("cfg: bad chat_id -> None", cfg3.admin_chat_id is None)
    ck("cfg: bad interval -> 60", cfg3.interval_sec == 60)
    ck("cfg: bad cpu -> 90", cfg3.cpu_threshold == 90.0)
    ck("cfg: empty mem -> 85", cfg3.mem_threshold == 85.0)

    # ---------- Model: вставка и подсчёт ----------
    async with sf() as s:
        s.add(SystemMetric(timestamp=_utcnow(), cpu_percent=1.0, mem_total_mb=100,
                           mem_used_mb=10, mem_percent=10, disk_total_gb=5,
                           disk_used_gb=1, disk_percent=20, net_bytes_sent=0,
                           net_bytes_recv=0, load_avg_1=0, load_avg_5=0, load_avg_15=0))
        await s.commit()
        cnt = (await s.execute(select(func.count(SystemMetric.id)))).scalar()
    ck("model: insert+count==1", cnt == 1)

    # ---------- collect_and_store: запись снимка ----------
    env_clear()
    svc = MonitorService(bot=FakeBot(), session_factory=sf)
    async with sf() as s:
        before = (await s.execute(select(func.count(SystemMetric.id)))).scalar()
    await svc.collect_and_store()
    async with sf() as s:
        after = (await s.execute(select(func.count(SystemMetric.id)))).scalar()
        last = (await s.execute(
            select(SystemMetric).order_by(SystemMetric.id.desc()).limit(1)
        )).scalar_one()
    ck("collect: row added", after == before + 1)
    ck("collect: cpu_percent is float", isinstance(last.cpu_percent, float))
    ck("collect: mem_total_mb>0", last.mem_total_mb > 0)
    ck("collect: net_bytes_recv>=0", last.net_bytes_recv >= 0)
    ck("collect: timestamp set", last.timestamp is not None)

    # В CI на полностью idle машине psutil.cpu_percent может вернуть 0.0,
    # из-за чего порог 0% не сработает. Фиксируем CPU на 100% для блока алертов.
    import app.services.monitor as _monitor_module

    _monitor_module.psutil.cpu_percent = lambda **kwargs: 100.0

    # ---------- Алерт: ниже порога -> ничего не шлём ----------
    bot = FakeBot()
    env_clear()
    env_set(admin_chat_id="111", cpu_threshold="999", mem_threshold="999", disk_threshold="999")
    svc2 = MonitorService(bot=bot, session_factory=sf, config=MonitorConfig.from_env())
    await svc2.collect_and_store()
    ck("alert: none below threshold", len(bot.sent) == 0)

    # ---------- Алерт: CPU выше порога -> 1 сообщение ----------
    bot2 = FakeBot()
    env_clear()
    env_set(admin_chat_id="111", cpu_threshold="0", mem_threshold="999",
            disk_threshold="999", cooldown_sec="1000", disk_path=".")
    svc3 = MonitorService(bot=bot2, session_factory=sf, config=MonitorConfig.from_env())
    await svc3.collect_and_store()
    ck("alert: cpu fired once", len(bot2.sent) == 1, f"sent={len(bot2.sent)}")
    if bot2.sent:
        ck("alert: chat_id correct", bot2.sent[0][0] == 111)
        ck("alert: text has CPU", "CPU" in bot2.sent[0][1])
    # кулдаун: повторный сбор в окне кулдауна -> нового сообщения нет
    await svc3.collect_and_store()
    ck("alert: cooldown blocks 2nd", len(bot2.sent) == 1, f"sent={len(bot2.sent)}")

    # ---------- Алерт: bot=None -> алерты выключены, без краха ----------
    svc4 = MonitorService(bot=None, session_factory=sf, config=MonitorConfig.from_env())
    await svc4.collect_and_store()
    ck("alert: bot None ok", True)

    # ---------- cleanup_old: удаляет старое, оставляет свежее ----------
    async with sf() as s:
        s.add(SystemMetric(timestamp=_utcnow() - timedelta(days=40), cpu_percent=1,
                           mem_total_mb=1, mem_used_mb=1, mem_percent=1, disk_total_gb=1,
                           disk_used_gb=1, disk_percent=1, net_bytes_sent=0, net_bytes_recv=0,
                           load_avg_1=0, load_avg_5=0, load_avg_15=0))
        await s.commit()
    env_clear()
    env_set(retention_days="30")
    svc5 = MonitorService(bot=None, session_factory=sf, config=MonitorConfig.from_env())
    deleted = await svc5.cleanup_old()
    ck("cleanup: deleted>=1", deleted >= 1, f"deleted={deleted}")
    async with sf() as s:
        remaining = (await s.execute(select(func.count(SystemMetric.id)))).scalar()
    ck("cleanup: recent remain", remaining >= 1)

    # ---------- БЕЗОПАСНОСТЬ: retention_days=0 не должен удалять свежие данные ----------
    env_clear()
    env_set(retention_days="0")
    async with sf() as s:
        fresh_before = (await s.execute(select(func.count(SystemMetric.id)))).scalar()
    svc5b = MonitorService(bot=None, session_factory=sf, config=MonitorConfig.from_env())
    await svc5b.cleanup_old()
    async with sf() as s:
        fresh_after = (await s.execute(select(func.count(SystemMetric.id)))).scalar()
    ck("sec: retention=0 keeps recent (guard max(1,...))",
       fresh_after >= fresh_before, f"before={fresh_before} after={fresh_after}")

    # ---------- БЕЗОПАСНОСТЬ: кулдаун НЕ обновляется при сбое отправки ----------
    class FailOnceBot:
        def __init__(self):
            self.sent = []
            self.calls = 0

        async def send_message(self, chat_id, text, parse_mode=None):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("telegram down")
            self.sent.append((chat_id, text, parse_mode))

    fb = FailOnceBot()
    env_clear()
    env_set(admin_chat_id="222", cpu_threshold="0", mem_threshold="999",
            disk_threshold="999", cooldown_sec="10000", disk_path=".")
    svc_fail = MonitorService(bot=fb, session_factory=sf, config=MonitorConfig.from_env())
    # 1-й сбор: send падает -> кулдаун НЕ должен зафиксироваться
    await svc_fail.collect_and_store()
    ck("sec: failed send -> no cooldown set (1st call attempted)",
       fb.calls == 1, f"calls={fb.calls}")
    ck("sec: failed send -> 0 delivered", len(fb.sent) == 0)
    # 2-й сбор: проблема та же -> алерт должен повториться (кулдауна нет)
    await svc_fail.collect_and_store()
    ck("sec: retry alert after failure -> delivered on 2nd",
       len(fb.sent) == 1, f"sent={len(fb.sent)}")

    # ---------- БЕЗОПАСНОСТЬ: HTML-экранирование server_name ----------
    bot_esc = FakeBot()
    env_clear()
    env_set(admin_chat_id="333", cpu_threshold="0", mem_threshold="999",
            disk_threshold="999", cooldown_sec="10000", server_name="a<b>&c`x",
            disk_path=".")
    svc_esc = MonitorService(bot=bot_esc, session_factory=sf, config=MonitorConfig.from_env())
    await svc_esc.collect_and_store()
    if bot_esc.sent:
        body = bot_esc.sent[0][1]
        ck("sec: server_name HTML-escaped (< -> &lt;)", "&lt;" in body, body)
        ck("sec: uses HTML parse_mode", bot_esc.sent[0][2] == "HTML")
    else:
        ck("sec: server_name escaped (no send — skipped)", False, "no alert sent")

    # ---------- ОПТИМИЗАЦИЯ: бенчмарк collect_and_store ----------
    import time as _t
    env_clear()
    svc_bench = MonitorService(bot=None, session_factory=sf)
    # 50 итераций, замер среднего времени
    n = 50
    t0 = _t.perf_counter()
    for _ in range(n):
        await svc_bench.collect_and_store()
    dt = _t.perf_counter() - t0
    per_call_ms = (dt / n) * 1000
    ck("perf: collect_and_store < 50ms/call (avg)", per_call_ms < 50,
       f"{per_call_ms:.2f}ms/call over {n} calls")
    # Размер БД после ~60+ записей должен быть скромным
    db_size_kb = tmp.stat().st_size / 1024
    ck("perf: sqlite small after 60+ rows (<1MB)", db_size_kb < 1024,
       f"{db_size_kb:.1f}KB")
    print(f"   [bench] {per_call_ms:.2f}ms/call, db={db_size_kb:.1f}KB after {n}+ rows")

    # ---------- FastAPI-роутер ----------
    import app.api.metrics_router as mr
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.include_router(mr.router)

    async def _gs():
        async with sf() as s:
            yield s

    app.dependency_overrides[mr.get_session] = _gs
    client = TestClient(app)
    ck("router: latest 200", client.get("/api/metrics/latest").status_code == 200)
    r2 = client.get("/api/metrics/history?hours=1")
    ck("router: history 200 list", r2.status_code == 200 and isinstance(r2.json(), list),
       f"code={r2.status_code}")
    r3 = client.get("/api/metrics/summary?hours=1")
    ck("router: summary 200", r3.status_code == 200, f"code={r3.status_code}")
    r4 = client.get("/api/metrics/history?hours=99999")
    ck("router: history bounds reject (>720 -> 422)", r4.status_code == 422, f"code={r4.status_code}")
    r5 = client.get("/api/metrics/history?hours=0")
    ck("router: history reject 0 -> 422", r5.status_code == 422, f"code={r5.status_code}")
    ck("router: latest has cpu field",
       "cpu_percent" in client.get("/api/metrics/latest").json())
    # Поля load_avg_5/15 теперь тоже отдаются (согласованность с моделью)
    latest_json = client.get("/api/metrics/latest").json()
    ck("router: latest has load_avg_5", "load_avg_5" in latest_json, str(latest_json.keys()))
    ck("router: latest has load_avg_15", "load_avg_15" in latest_json, str(latest_json.keys()))

    # ---------- Безопасность: токен-аутентификация ----------
    os.environ["MONITOR_API_TOKEN"] = "secret-token-xyz"  # nosec B105 — тестовый токен
    ck("sec: no token header -> 401",
       client.get("/api/metrics/latest").status_code == 401)
    ck("sec: wrong token -> 401",
       client.get("/api/metrics/latest", headers={"X-Monitor-Token": "nope"}).status_code == 401)
    ck("sec: correct token -> 200",
       client.get("/api/metrics/latest", headers={"X-Monitor-Token": "secret-token-xyz"}).status_code == 200)
    ck("sec: history with token -> 200",
       client.get("/api/metrics/history?hours=1",
                  headers={"X-Monitor-Token": "secret-token-xyz"}).status_code == 200)
    del os.environ["MONITOR_API_TOKEN"]
    ck("sec: token unset -> open again 200",
       client.get("/api/metrics/latest").status_code == 200)

    # ---------- Безопасность: потолок числа точек (/history) ----------
    saved_cap = mr.MAX_HISTORY_POINTS
    mr.MAX_HISTORY_POINTS = 3
    async with sf() as s:
        s.add_all([SystemMetric(timestamp=_utcnow() - timedelta(minutes=i),
                                cpu_percent=float(i), mem_total_mb=1, mem_used_mb=1,
                                mem_percent=1, disk_total_gb=1, disk_used_gb=1,
                                disk_percent=1, net_bytes_sent=0, net_bytes_recv=0,
                                load_avg_1=0, load_avg_5=0, load_avg_15=0)
                   for i in range(5)])
        await s.commit()
    hc = client.get("/api/metrics/history?hours=24").json()
    ck("sec: history capped to MAX_HISTORY_POINTS", len(hc) == 3, f"got {len(hc)}")
    ck("sec: history chronological order",
       len(hc) < 2 or hc[0]["timestamp"] <= hc[-1]["timestamp"])
    mr.MAX_HISTORY_POINTS = saved_cap

    # ---------- Устойчивость: ошибка БД не валит процесс ----------
    class BadSF:
        def __call__(self):
            raise RuntimeError("boom")

    svc6 = MonitorService(bot=FakeBot(), session_factory=BadSF())
    try:
        await svc6.collect_and_store()
        ck("resilience: db error swallowed", True)
    except Exception as e:
        ck("resilience: db error swallowed", False, repr(e))

    # ---------- PRAGMA-оптимизация SQLite (configure_sqlite_for_monitoring) ----------
    from app.integration.scheduler_setup import configure_sqlite_for_monitoring
    from sqlalchemy import text
    eng2 = create_async_engine(f"sqlite+aiosqlite:///{(pathlib.Path(tempfile.gettempdir()) / 'mon_pragma.db').as_posix()}")
    configure_sqlite_for_monitoring(eng2)
    async with eng2.connect() as conn:
        mode = (await conn.execute(text("PRAGMA journal_mode"))).scalar()
        sync = (await conn.execute(text("PRAGMA synchronous"))).scalar()
        timeout = (await conn.execute(text("PRAGMA busy_timeout"))).scalar()
    await eng2.dispose()
    ck("pragma: WAL journal_mode set", str(mode).lower() == "wal", f"got {mode!r}")
    ck("pragma: synchronous=NORMAL", int(sync) == 1, f"got {sync!r} (1=NORMAL)")
    ck("pragma: busy_timeout=5000", int(timeout) == 5000, f"got {timeout!r}")

    # ---------- Model default timestamp: НЕ должен вызывать deprecated utcnow ----------
    import warnings as _w
    with _w.catch_warnings():
        _w.simplefilter("error", DeprecationWarning)
        try:
            m = SystemMetric(cpu_percent=1, mem_total_mb=1, mem_used_mb=1,
                             mem_percent=1, disk_total_gb=1, disk_used_gb=1,
                             disk_percent=1, net_bytes_sent=0, net_bytes_recv=0,
                             load_avg_1=0, load_avg_5=0, load_avg_15=0)
            # Триггерим вычисление default (SQLAlchemy вычисляет default при flush/access)
            _ts = m.timestamp
            ck("model: default timestamp no DeprecationWarning", True)
        except DeprecationWarning as e:
            ck("model: default timestamp no DeprecationWarning", False, str(e))

    # ---------- SUMMARY ----------
    passed = sum(1 for _, c, _ in R if c)
    print("\n==== SUMMARY ====")
    print(f"{passed}/{len(R)} passed")
    failed = [n for n, c, _ in R if not c]
    if failed:
        print("FAILED:", failed)
    return passed == len(R)


def test_monitor_main() -> None:
    """Pytest-обёртка над скриптовым дебаг-тестом."""
    assert asyncio.run(main())


if __name__ == "__main__":
    ok = asyncio.run(main())
    sys.exit(0 if ok else 1)
