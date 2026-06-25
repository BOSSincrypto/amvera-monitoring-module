"""
Sqladmin-представление для просмотра метрик в веб-админке.

ИНТЕГРАЦИЯ:
----------
from app.admin.system_metric_admin import SystemMetricAdmin
admin.add_view(SystemMetricAdmin)

где `admin = Admin(...)` — ваш экземпляр sqladmin.
Для графиков используйте api/metrics_router.py — sqladmin показывает таблицу.
"""

from __future__ import annotations

from app.models.system_metric import SystemMetric  # !!! поправьте путь импорта
from sqladmin import ModelView


class SystemMetricAdmin(ModelView, model=SystemMetric):
    name = "Метрики сервера"
    name_plural = "Метрики сервера"
    icon = "fa-solid fa-chart-line"

    column_list = [
        SystemMetric.timestamp,
        SystemMetric.cpu_percent,
        SystemMetric.mem_percent,
        SystemMetric.disk_percent,
        SystemMetric.load_avg_1,
    ]
    column_default_sort = ("timestamp", True)  # последние сверху
    column_searchable_list = [SystemMetric.timestamp]
    column_labels = {
        SystemMetric.timestamp: "Время (UTC)",
        SystemMetric.cpu_percent: "CPU %",
        SystemMetric.mem_percent: "RAM %",
        SystemMetric.mem_used_mb: "RAM, МБ",
        SystemMetric.mem_total_mb: "RAM всего, МБ",
        SystemMetric.disk_percent: "Disk %",
        SystemMetric.disk_used_gb: "Disk, ГБ",
        SystemMetric.disk_total_gb: "Disk всего, ГБ",
        SystemMetric.net_bytes_sent: "Net sent, б",
        SystemMetric.net_bytes_recv: "Net recv, б",
        SystemMetric.load_avg_1: "Load 1m",
        SystemMetric.load_avg_5: "Load 5m",
        SystemMetric.load_avg_15: "Load 15m",
    }
    page_size = 100
    can_create = False
    can_edit = False
    can_delete = True
