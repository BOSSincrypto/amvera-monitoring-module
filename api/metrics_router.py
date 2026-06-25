"""
FastAPI-роутер отдачи метрик в JSON — для графиков вашего дашборда.

ИНТЕГРАЦИЯ (если в проекте есть FastAPI):
-----------------------------------------
from app.api.metrics_router import router as metrics_router
app.include_router(metrics_router)

БЕЗОПАСНОСТЬ: эндпоинты защищены опциональным токеном MONITOR_API_TOKEN.
Если переменная задана — каждый запрос обязан нести заголовок
`X-Monitor-Token: <значение>` (сравнение за константное время).
Если НЕ задана — доступ открыт (для публичного URL Amvera обязательно
задайте токен). Альтернативно можно навесить ваш auth-миддлвар (тот же,
что защищает sqladmin).
"""

from __future__ import annotations

import logging
import os
import secrets
from datetime import datetime, timedelta, timezone

# !!! поправьте путь импорта под структуру вашего проекта !!!
from app.models.system_metric import SystemMetric
from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger("monitor.api")

router = APIRouter(prefix="/api/metrics", tags=["metrics"])

# Жёсткий потолок числа точек в /history — защита от тяжёлого ответа (DoS).
MAX_HISTORY_POINTS = 5000


def _configured_token() -> str | None:
    """Токен доступа из MONITOR_API_TOKEN (None — токен не задан)."""
    raw = os.getenv("MONITOR_API_TOKEN")
    return raw.strip() if raw and raw.strip() else None


async def require_token(
    x_monitor_token: str | None = Header(default=None, alias="X-Monitor-Token"),
) -> None:
    """
    Защита эндпоинтов токеном.

    * MONITOR_API_TOKEN задан  -> каждый запрос обязан нести заголовок
      `X-Monitor-Token: <тот же токен>` (сравнение за константное время,
      чтобы исключить timing-атаку на перебор токена).
    * MONITOR_API_TOKEN НЕ задан -> доступ открыт (на ответственность оператора;
      это позволяет работать «из коробки», но для публичного URL Amvera
      настоятельно рекомендуется задать токен).
    """
    expected = _configured_token()
    if not expected:
        return  # токен не сконфигурирован — режим «по умолчанию открыт»
    provided = x_monitor_token or ""
    if not secrets.compare_digest(provided.encode("utf-8"), expected.encode("utf-8")):
        raise HTTPException(status_code=401, detail="Invalid or missing token")


# !!! ЗАМЕНИТЕ на вашу зависимость получения сессии !!!
# Обычно это async_sessionmaker, обёрнутая в Depends.
async def get_session() -> AsyncSession:  # pragma: no cover
    raise NotImplementedError(
        "Подключите вашу зависимость сессии SQLAlchemy, "
        "напр.: async def get_session(): async with session_factory() as s: yield s"
    )


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _round(v) -> float | None:
    return None if v is None else round(float(v), 2)


def _serialize(r: SystemMetric) -> dict:
    return {
        "timestamp": r.timestamp.isoformat() + "Z",
        "cpu_percent": round(r.cpu_percent, 2),
        "mem_percent": round(r.mem_percent, 2),
        "mem_used_mb": round(r.mem_used_mb, 1),
        "mem_total_mb": round(r.mem_total_mb, 1),
        "disk_percent": round(r.disk_percent, 2),
        "disk_used_gb": round(r.disk_used_gb, 2),
        "disk_total_gb": round(r.disk_total_gb, 2),
        "net_bytes_sent": r.net_bytes_sent,
        "net_bytes_recv": r.net_bytes_recv,
        "load_avg_1": round(r.load_avg_1, 2),
        "load_avg_5": round(r.load_avg_5, 2),
        "load_avg_15": round(r.load_avg_15, 2),
    }


@router.get("/latest")
async def latest(
    session: AsyncSession = Depends(get_session),
    _: None = Depends(require_token),
) -> dict:
    """Последний снимок метрик — для виджета «сейчас»."""
    stmt = (
        select(SystemMetric)
        .order_by(SystemMetric.timestamp.desc())
        .limit(1)
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "No metrics yet")
    return _serialize(row)


@router.get("/history")
async def history(
    hours: int = Query(24, ge=1, le=24 * 30),
    session: AsyncSession = Depends(get_session),
    _: None = Depends(require_token),
) -> list[dict]:
    """Список точек за период — для линейного графика.

    Число точек ограничено MAX_HISTORY_POINTS (защита от тяжёлого ответа):
    возвращаются ПОСЛЕДНИЕ точки периода в хронологическом порядке.
    """
    since = _utcnow() - timedelta(hours=hours)
    stmt = (
        select(SystemMetric)
        .where(SystemMetric.timestamp >= since)
        .order_by(SystemMetric.timestamp.desc())
        .limit(MAX_HISTORY_POINTS)
    )
    rows = list((await session.execute(stmt)).scalars().all())
    rows.reverse()  # хронологический порядок
    return [_serialize(r) for r in rows]


@router.get("/summary")
async def summary(
    hours: int = Query(24, ge=1, le=24 * 30),
    session: AsyncSession = Depends(get_session),
    _: None = Depends(require_token),
) -> dict:
    """Аггрегаты (avg/max) по CPU/RAM/Disk за период."""
    since = _utcnow() - timedelta(hours=hours)
    stmt = select(
        func.avg(SystemMetric.cpu_percent).label("cpu_avg"),
        func.max(SystemMetric.cpu_percent).label("cpu_max"),
        func.avg(SystemMetric.mem_percent).label("mem_avg"),
        func.max(SystemMetric.mem_percent).label("mem_max"),
        func.avg(SystemMetric.disk_percent).label("disk_avg"),
        func.max(SystemMetric.disk_percent).label("disk_max"),
        func.count(SystemMetric.id).label("samples"),
    ).where(SystemMetric.timestamp >= since)
    row = (await session.execute(stmt)).one()
    return {
        "hours": hours,
        "samples": int(row.samples or 0),
        "cpu_avg": _round(row.cpu_avg),
        "cpu_max": _round(row.cpu_max),
        "mem_avg": _round(row.mem_avg),
        "mem_max": _round(row.mem_max),
        "disk_avg": _round(row.disk_avg),
        "disk_max": _round(row.disk_max),
    }
