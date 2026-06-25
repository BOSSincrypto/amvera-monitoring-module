"""
Регистрация задач мониторинга в вашем AsyncIOScheduler.

ИНТЕГРАЦИЯ (пример):
--------------------
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from app.integration.scheduler_setup import register_monitor_jobs

scheduler = AsyncIOScheduler()
register_monitor_jobs(scheduler, bot=bot, session_factory=session_factory)
scheduler.start()
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from app.services.monitor import MonitorConfig, MonitorService  # !!! поправьте путь
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy.ext.asyncio import AsyncEngine

logger = logging.getLogger("monitor")

# Idempotency guard: множество id(engine), для которых PRAGMA-оптимизация уже
# применена. SQLAlchemy AsyncEngine использует __slots__ и не позволяет setattr
# произвольных атрибутов на сам объект, поэтому храним флаг здесь.
# Чистится автоматически при пересоздании процесса (модуль перезагружается).
_configured_engines: set[int] = set()


def configure_sqlite_for_monitoring(engine: AsyncEngine) -> None:
    """
    Опциональная production-оптимизация для SQLite-движка мониторинга.

    Включает:
      * WAL journal_mode — конкурентность чтения/записи, нет блокировок
        при одновременном /api/metrics и записи снимка;
      * synchronous=NORMAL — ускорение записи в ~2-3 раза (достаточно при WAL);
      * busy_timeout=5000 — ждать до 5с при lock вместо сразу падать.

    Вызовите ОДИН раз после создания engine:
        from sqlalchemy.ext.asyncio import create_async_engine
        engine = create_async_engine("sqlite+aiosqlite:////data/app.db")
        configure_sqlite_for_monitoring(engine)

    Idempotent: повторный вызов на том же engine — no-op (защита от
    случайной двойной регистрации listener'ов при reload/ошибке интегратора).
    Безопасно для не-SQLite движков (просто нет эффекта).
    """
    # Idempotency guard через module-level set (engine — __slots__, setattr запрещён).
    eng_id = id(engine)
    if eng_id in _configured_engines:
        return

    # Безопасность: PRAGMA работает только в SQLite. На PostgreSQL/MySQL
    # эти команды вызовут SyntaxError при каждом новом соединении.
    if engine.dialect.name != "sqlite":
        logger.debug(
            "configure_sqlite_for_monitoring: dialect=%s is not SQLite, skipping PRAGMAs",
            engine.dialect.name,
        )
        _configured_engines.add(eng_id)  # отмечаем как обработанный (no-op)
        return

    from sqlalchemy import event

    @event.listens_for(engine.sync_engine, "connect")
    def _set_pragmas(dbapi_conn, _):
        cur = dbapi_conn.cursor()
        try:
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.execute("PRAGMA busy_timeout=5000")
        finally:
            cur.close()

    _configured_engines.add(eng_id)
    logger.info("SQLite PRAGMAs configured (WAL, synchronous=NORMAL, busy_timeout=5000)")


def build_monitor_service(bot, session_factory) -> MonitorService:
    """Создаёт MonitorService с конфигом из env."""
    return MonitorService(
        bot=bot,
        session_factory=session_factory,
        config=MonitorConfig.from_env(),
    )


def register_monitor_jobs(
    scheduler: AsyncIOScheduler,
    bot,
    session_factory,
) -> MonitorService:
    """
    Создаёт MonitorService и вешает две задачи:
      * сбор метрик — по интервалу MONITOR_INTERVAL_SEC (первый запуск сразу);
      * очистка старых метрик — раз в сутки в 03:00 UTC.

    Parameters
    ----------
    bot: aiogram.Bot (может быть None — алерты выключатся).
    session_factory: async_sessionmaker вашего приложения.
    """
    service = build_monitor_service(bot, session_factory)
    cfg = service.config

    now_utc = datetime.now(timezone.utc)

    scheduler.add_job(
        service.collect_and_store,
        trigger=IntervalTrigger(seconds=max(15, cfg.interval_sec), start_date=now_utc),
        id="monitor:collect",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=60,
    )

    scheduler.add_job(
        service.cleanup_old,
        trigger=CronTrigger(hour=3, minute=0, timezone="UTC"),
        id="monitor:cleanup",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    actual_interval = max(15, cfg.interval_sec)
    logger.info(
        "Monitor jobs registered: collect every %ds, cleanup at 03:00 UTC",
        actual_interval,
    )
    return service
