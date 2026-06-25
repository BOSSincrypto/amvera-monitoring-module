"""
Модель хранения метрик контейнера для модуля мониторинга (Amvera).

ИНТЕГРАЦИЯ:
----------
1) ЗАМЕНИТЕ импорт Base ниже на ваш реальный declarative base
   (там, где `class Base(DeclarativeBase): ...`).
2) Эта модель должна попасть в Base.metadata ДО первого
   `await conn.run_sync(Base.metadata.create_all)`.
   Простейший способ — добавить строку
       from app.models.system_metric import SystemMetric  # noqa: F401
   в ваш models/__init__.py (или куда у вас собираются все модели).
3) SQLite-файл должен лежать на постоянном хранилище Amvera (/data),
   иначе история метрик потеряется при перезапуске/пересборке.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import BigInteger, Float, Integer
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# !!! УДАЛИТЕ ЭТУ ЗАГЛУШКУ И РАСКОММЕНТИРУЙТЕ ИМПОРТ ВАШЕГО Base !!!
# from app.db.base import Base   # <-- раскомментируйте и поправьте путь


class Base(DeclarativeBase):
    """Временная заглушка. Удалите её и импортируйте свой Base выше."""


def _utcnow() -> datetime:
    """Текущее UTC-время как naive datetime (для хранения в БД). Не deprecated."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class SystemMetric(Base):
    """Один сэмпл системных метрик контейнера (снимается раз в минуту)."""

    __tablename__ = "system_metrics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    timestamp: Mapped[datetime] = mapped_column(
        default=_utcnow, index=True, nullable=False
    )

    # --- CPU ---
    cpu_percent: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    # --- Memory (МБ) ---
    mem_total_mb: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    mem_used_mb: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    mem_percent: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    # --- Disk (ГБ) — постоянное хранилище Amvera (/data) ---
    disk_total_gb: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    disk_used_gb: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    disk_percent: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    # --- Network (байт с момента старта контейнера) ---
    net_bytes_sent: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    net_bytes_recv: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)

    # --- Load average (1 / 5 / 15 мин). В контейнере отражает хост! ---
    load_avg_1: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    load_avg_5: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    load_avg_15: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"<SystemMetric t={self.timestamp:%Y-%m-%d %H:%M:%S} "
            f"cpu={self.cpu_percent:.1f}% mem={self.mem_percent:.1f}% "
            f"disk={self.disk_percent:.1f}%>"
        )
