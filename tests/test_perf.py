"""Профайлинг: RAM, время, поведение на большом объёме данных.

Запуск:  python tests/test_perf.py
"""
import asyncio
import os
import pathlib
import sys
import tempfile
import time
import tracemalloc
import types

HERE = os.path.dirname(os.path.abspath(__file__))
MON = os.path.dirname(HERE)
_app = types.ModuleType("app")
_app.__path__ = [MON]
sys.modules["app"] = _app

import psutil
from app.models.system_metric import Base, SystemMetric
from app.services.monitor import MonitorService, _utcnow
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


def mb(b):
    return b / 1048576.0


async def main():
    tmp = pathlib.Path(tempfile.gettempdir()) / "mon_perf.db"
    if tmp.exists():
        tmp.unlink()
    eng = create_async_engine(f"sqlite+aiosqlite:///{tmp.as_posix()}")
    async with eng.begin() as c:
        await c.run_sync(Base.metadata.create_all)
    sf = async_sessionmaker(eng, expire_on_commit=False)
    proc = psutil.Process()
    svc = MonitorService(bot=None, session_factory=sf)

    print("=" * 60)
    print("ПРОФАЙЛ РЕСУРСОВ МОДУЛЯ МОНИТОРИНГА")
    print("=" * 60)

    # 1) RSS процесса (baseline = всё приложение с импортами)
    rss_base = proc.memory_info().rss
    print(f"RSS процесса (база, с импортами): {mb(rss_base):.1f} МБ")

    # 2) tracemalloc: чистая аллокация Python за 1 collect_and_store
    tracemalloc.start()
    _ = tracemalloc.take_snapshot()
    await svc.collect_and_store()
    _ = tracemalloc.take_snapshot()
    cur, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    print(f"Аллокации Python за 1 снимок:  cur={mb(cur):.3f} МБ  peak={mb(peak):.3f} МБ")

    # 3) Время 100 снимков (холодный + тёплый)
    n = 100
    t0 = time.perf_counter()
    for _ in range(n):
        await svc.collect_and_store()
    dt = time.perf_counter() - t0
    print(f"Время {n} снимков: {dt*1000:.0f} мс всего, {dt/n*1000:.2f} мс/снимок")

    async with sf() as s:
        cnt = (await s.execute(select(SystemMetric.id))).all()
    print(f"Записей в БД: {len(cnt)}")

    # 4) Большой объём: вставляем 10 000 строк, замеряем cleanup и /history
    print("\n--- Стресс: 10 000 строк ---")
    batch = [
        SystemMetric(timestamp=_utcnow(), cpu_percent=50.0, mem_total_mb=512,
                     mem_used_mb=256, mem_percent=50, disk_total_gb=10,
                     disk_used_gb=5, disk_percent=50, net_bytes_sent=1000,
                     net_bytes_recv=2000, load_avg_1=1, load_avg_5=1, load_avg_15=1)
        for _ in range(10000)
    ]
    t0 = time.perf_counter()
    async with sf() as s:
        s.add_all(batch)
        await s.commit()
    ins_t = time.perf_counter() - t0
    db_size = tmp.stat().st_size / 1024
    print(f"Вставка 10k строк: {ins_t*1000:.0f} мс, размер БД: {db_size:.0f} КБ")

    # cleanup
    t0 = time.perf_counter()
    deleted = await svc.cleanup_old()
    cl_t = time.perf_counter() - t0
    print(f"Cleanup (retention=30d): удалено {deleted} за {cl_t*1000:.0f} мс")

    # 5) RSS после большого объёма (проверка на утечку)
    rss_after = proc.memory_info().rss
    print(f"RSS после стресса: {mb(rss_after):.1f} МБ (дельта {mb(rss_after-rss_base):+.1f} МБ)")

    # 6) Итоговый размер БД
    print(f"Финальный размер БД: {tmp.stat().st_size/1024:.0f} КБ")

    await eng.dispose()
    print("=" * 60)
    print("ВЫВОД: см. отчёт выше — все числа должны быть компактными.")


def test_perf_main() -> None:
    """Pytest-обёртка над профайлингом: не падает и выполняет все замеры."""
    asyncio.run(main())


if __name__ == "__main__":
    asyncio.run(main())
