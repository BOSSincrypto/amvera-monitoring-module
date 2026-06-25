"""
Сервис мониторинга ресурсов контейнера для Amvera (Python pip-среда).

Зависимости: psutil (см. requirements.txt).
Интегрируется в существующее aiogram-приложение:
  * бот отправляет алерты в Telegram;
  * APScheduler дёргает collect_and_store() раз в минуту;
  * SQLAlchemy хранит историю для графиков дашборда.

Footprint: ~2-3 МБ RAM (psutil), один снимок метрик в минуту.
"""

from __future__ import annotations

import asyncio
import html
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import cast

import psutil

# !!! поправьте путь импорта под структуру вашего проекта !!!
from app.models.system_metric import SystemMetric
from sqlalchemy import CursorResult, delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = logging.getLogger("monitor")


# Время на сервере Amvera = UTC. Храним naive datetime в UTC.
def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _env_int(key: str, default: int) -> int:
    raw = os.getenv(key)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        logger.warning("Bad int env %s=%r, using default %d", key, raw, default)
        return default


def _env_float(key: str, default: float) -> float:
    raw = os.getenv(key)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        logger.warning("Bad float env %s=%r, using default %f", key, raw, default)
        return default


def _env_bool(key: str, default: bool) -> bool:
    raw = os.getenv(key)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class MonitorConfig:
    admin_chat_id: int | None
    server_name: str
    interval_sec: int
    retention_days: int
    cpu_threshold: float
    mem_threshold: float
    disk_threshold: float
    disk_path: str
    cooldown_sec: float
    collect_load: bool

    @classmethod
    def from_env(cls) -> MonitorConfig:
        chat_raw = os.getenv("MONITOR_ADMIN_CHAT_ID", "").strip()
        admin_chat_id: int | None = None
        if chat_raw:
            try:
                admin_chat_id = int(chat_raw)
            except ValueError:
                logger.warning(
                    "MONITOR_ADMIN_CHAT_ID=%r is not int, alerts disabled", chat_raw
                )
        return cls(
            admin_chat_id=admin_chat_id,
            server_name=os.getenv("MONITOR_SERVER_NAME", "amvera"),
            interval_sec=_env_int("MONITOR_INTERVAL_SEC", 60),
            retention_days=_env_int("MONITOR_RETENTION_DAYS", 30),
            cpu_threshold=_env_float("MONITOR_CPU_THRESHOLD", 90.0),
            mem_threshold=_env_float("MONITOR_MEM_THRESHOLD", 85.0),
            disk_threshold=_env_float("MONITOR_DISK_THRESHOLD", 90.0),
            disk_path=os.getenv("MONITOR_DISK_PATH", "/data"),
            cooldown_sec=_env_float("MONITOR_ALERT_COOLDOWN_SEC", 300.0),
            collect_load=_env_bool("MONITOR_COLLECT_LOAD", True),
        )


class MonitorService:
    """
    Собирает метрики контейнера, пишет в БД и шлёт алерты в Telegram.

    Parameters
    ----------
    bot:
        объект aiogram Bot (для отправки алертов) или None — тогда алерты выключены.
    session_factory:
        async_sessionmaker[AsyncSession] вашего приложения.
    config:
        MonitorConfig (по умолчанию читается из env).
    """

    def __init__(
        self,
        bot,
        session_factory: async_sessionmaker[AsyncSession],
        config: MonitorConfig | None = None,
    ) -> None:
        self.bot = bot
        self.session_factory = session_factory
        self.config = config or MonitorConfig.from_env()
        # Кулдаун алертов в памяти: metric_key -> время последнего алерта
        self._last_alert: dict[str, float] = {}
        # Разогрев счётчика CPU (первый вызов psutil.cpu_percent даёт 0.0)
        try:
            psutil.cpu_percent(interval=None)
        except Exception:  # pragma: no cover
            logger.debug("cpu_percent warmup failed", exc_info=True)

    # ------------------------------------------------------------------
    # Сбор метрик (синхронно, выполняется в отдельном потоке)
    # ------------------------------------------------------------------
    def _collect_snapshot(self) -> dict:
        cfg = self.config
        data: dict = {}

        # CPU
        data["cpu_percent"] = float(psutil.cpu_percent(interval=None))

        # Memory
        mem = psutil.virtual_memory()
        data["mem_total_mb"] = mem.total / 1048576.0
        data["mem_used_mb"] = mem.used / 1048576.0
        data["mem_percent"] = float(mem.percent)

        # Disk (постоянное хранилище Amvera)
        try:
            disk = psutil.disk_usage(cfg.disk_path)
            data["disk_total_gb"] = disk.total / 1073741824.0
            data["disk_used_gb"] = disk.used / 1073741824.0
            data["disk_percent"] = float(disk.percent)
        except OSError:
            # пути /data может не быть при локальном тесте
            data["disk_total_gb"] = 0.0
            data["disk_used_gb"] = 0.0
            data["disk_percent"] = 0.0

        # Network
        net = psutil.net_io_counters()
        data["net_bytes_sent"] = int(net.bytes_sent)
        data["net_bytes_recv"] = int(net.bytes_recv)

        # Load average (опционально; в контейнере отражает хост!)
        if cfg.collect_load and hasattr(os, "getloadavg"):
            load = os.getloadavg()
            data["load_avg_1"] = float(load[0])
            data["load_avg_5"] = float(load[1])
            data["load_avg_15"] = float(load[2])
        else:
            data["load_avg_1"] = data["load_avg_5"] = data["load_avg_15"] = 0.0

        return data

    # ------------------------------------------------------------------
    # Основная точка входа для APScheduler
    # ------------------------------------------------------------------
    async def collect_and_store(self) -> None:
        """Снять метрики, сохранить в БД, проверить пороги и отправить алерты."""
        try:
            data = await asyncio.to_thread(self._collect_snapshot)
        except Exception:
            logger.exception("Failed to collect metrics snapshot")
            return

        metric = SystemMetric(timestamp=_utcnow(), **data)

        try:
            async with self.session_factory() as session:
                session.add(metric)
                await session.commit()
        except Exception:
            logger.exception("Failed to persist SystemMetric")
            return

        try:
            await self._check_and_alert(metric)
        except Exception:
            logger.exception("Alerting failed")

    # ------------------------------------------------------------------
    # Алерты в Telegram
    # ------------------------------------------------------------------
    async def _check_and_alert(self, m: SystemMetric) -> None:
        cfg = self.config
        if cfg.admin_chat_id is None or self.bot is None:
            return  # алерты выключены

        now = time.monotonic()
        # server_name экранируем (он из env, но защита от случайной инъекции разметки)
        server = html.escape(cfg.server_name, quote=False)
        checks = [
            (
                "cpu",
                m.cpu_percent >= cfg.cpu_threshold,
                f"🔴 CPU {m.cpu_percent:.1f}% "
                f"(порог {cfg.cpu_threshold:.0f}%)",
            ),
            (
                "mem",
                m.mem_percent >= cfg.mem_threshold,
                f"🔴 RAM {m.mem_percent:.1f}% "
                f"({m.mem_used_mb:.0f}/{m.mem_total_mb:.0f} МБ, "
                f"порог {cfg.mem_threshold:.0f}%)",
            ),
            (
                "disk",
                m.disk_percent >= cfg.disk_threshold,
                f"🔴 Disk {m.disk_percent:.1f}% "
                f"({m.disk_used_gb:.1f}/{m.disk_total_gb:.1f} ГБ, "
                f"порог {cfg.disk_threshold:.0f}%)",
            ),
        ]

        # Собираем кандидатов: сработали И вышли из окна кулдауна.
        # Кулдаун обновляем ТОЛЬКО после успешной отправки, иначе единичный
        # сбой Telegram привёл бы к пропуску алертов при сохранении проблемы.
        candidates: list[tuple[str, str]] = []
        for key, triggered, msg in checks:
            if not triggered:
                continue
            last_alert = self._last_alert.get(key)
            if last_alert is not None and now - last_alert < cfg.cooldown_sec:
                continue
            candidates.append((key, msg))

        if not candidates:
            return

        text = (
            "🚨 <b>Алерт мониторинга</b>\n"
            f"Сервер: <code>{server}</code>\n"
            + "\n".join(msg for _, msg in candidates)
            + f"\n⏱ {m.timestamp:%Y-%m-%d %H:%M:%S} UTC"
        )
        try:
            await self.bot.send_message(
                cfg.admin_chat_id, text, parse_mode="HTML"
            )
        except Exception:
            logger.exception("Telegram send_message failed")
            return  # не обновляем кулдаун — повторим алерт на следующем сборе

        # Успешная отправка: фиксируем кулдаун для всех отосланных ключей.
        for key, _ in candidates:
            self._last_alert[key] = now

    # ------------------------------------------------------------------
    # Очистка старых метрик (вызывать раз в сутки)
    # ------------------------------------------------------------------
    async def cleanup_old(self) -> int:
        """Удалить метрики старше retention_days. Возвращает число удалённых строк."""
        cfg = self.config
        # Защита от случайного удаления свежих данных: минимум 1 день
        # (retention_days <= 0 обнулил бы cutoff и удалил бы всю историю).
        days = max(1, cfg.retention_days)
        cutoff = _utcnow() - timedelta(days=days)
        try:
            async with self.session_factory() as session:
                result = cast(
                    CursorResult,
                    await session.execute(
                        delete(SystemMetric).where(SystemMetric.timestamp < cutoff)
                    ),
                )
                await session.commit()
                rc = result.rowcount
                deleted = rc if (rc is not None and rc > 0) else 0
                if deleted:
                    logger.info(
                        "Cleaned up %d old metrics (older than %s)", deleted, cutoff
                    )
                return deleted
        except Exception:
            logger.exception("Cleanup of old metrics failed")
            return 0
