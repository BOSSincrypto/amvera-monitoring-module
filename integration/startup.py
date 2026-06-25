"""
Пример инициализации мониторинга при старте aiogram-бота.

Этот файл — ШАБЛОН. Скопируйте нужные строки в ваш код инициализации
(обычно туда, где создаётся Bot, Dispatcher и AsyncIOScheduler).
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


async def on_startup_monitoring(
    bot,
    scheduler,
    session_factory,
) -> None:
    """
    Вызвать при старте приложения (dp.startup / FastAPI lifespan).

    bot:           aiogram.Bot
    scheduler:     AsyncIOScheduler (ещё не запущенный или уже запущенный — ок)
    session_factory: async_sessionmaker вашего приложения
    """
    from app.integration.scheduler_setup import register_monitor_jobs  # !!! путь

    service = register_monitor_jobs(
        scheduler, bot=bot, session_factory=session_factory
    )
    logger.info(
        "Monitoring started: chat_id=%s server=%s interval=%ds",
        service.config.admin_chat_id,
        service.config.server_name,
        service.config.interval_sec,
    )


# ---------------------------------------------------------------------
# Минимальный пример подключения в __main__ / bot.py (ЗАКОММЕНТИРОВАНО):
# ---------------------------------------------------------------------
# from aiogram import Bot, Dispatcher
# from apscheduler.schedulers.asyncio import AsyncIOScheduler
#
# bot = Bot(token=BOT_TOKEN)
# dp = Dispatcher()
# scheduler = AsyncIOScheduler()
#
# from app.integration.startup import on_startup_monitoring
#
# @dp.startup()
# async def _on_startup():
#     # session_factory = ваша async_sessionmaker (sqlite на /data)
#     await on_startup_monitoring(bot, scheduler, session_factory)
#     scheduler.start()
#
# if __name__ == "__main__":
#     import asyncio
#     asyncio.run(dp.start_polling(bot))
