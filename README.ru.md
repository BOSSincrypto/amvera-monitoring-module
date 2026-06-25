# Модуль мониторинга сервера (Amvera) для aiogram-приложения

Лёгкий встроенный мониторинг ресурсов контейнера с алертами в Telegram.
**+0 отдельных процессов, +1 зависимость (psutil), +~2-3 МБ RAM, +0 ₽/мес на Amvera.**

---

## 📌 Что делает

Раз в минуту снимает метрики контейнера (CPU / RAM / Disk `/data` / Network / load average),
сохраняет историю в SQLite (через SQLAlchemy), и при превышении порогов шлёт алерт в
Telegram-чат через вашего aiogram-бота. Метрики отдаются в JSON через FastAPI-эндпоинты
для графиков вашего дашборда и видны в sqladmin.

## 🧱 Стек (ваш)

- **aiogram 3** — отправка алертов (`bot.send_message`)
- **SQLAlchemy 2.0 async + aiosqlite** — хранение метрик
- **APScheduler (AsyncIOScheduler)** — запуск сбора по расписанию
- **sqladmin** — просмотр метрик в веб-админке
- **psutil** — единственная НОВАЯ зависимость

## 📂 Структура модуля

```
monitoring-module/
├── README.md                     ← этот файл
├── requirements.txt              ← доп. зависимость: psutil==6.1.0
├── .env.example                  ← все переменные окружения
├── amvera.yml.example            ← пример конфига Amvera (pip-среда)
├── models/
│   └── system_metric.py          ← модель SQLAlchemy SystemMetric
├── services/
│   └── monitor.py                ← MonitorService: сбор + запись + алерты
├── admin/
│   └── system_metric_admin.py    ← sqladmin-представление
├── api/
│   └── metrics_router.py         ← FastAPI-роутер JSON для дашборда
├── integration/
│   ├── scheduler_setup.py        ← регистрация задач + configure_sqlite_for_monitoring()
│   └── startup.py                ← пример on_startup для aiogram
└── tests/
    ├── test_monitor.py           ← 54 функциональных/security-теста
    ├── test_edge.py              ← 14 edge-case тестов (границы, idempotency, пустая БД)
    └── test_perf.py              ← профайлинг RAM/времени/объёма
```

## ⚡ Оптимизация (проверено профайлингом)

Замеры (`tests/test_perf.py`):
- **~7.7 мс / снимок** метрик (сбор psutil + запись SQLite)
- **~0.18 МБ** аллокаций Python на один снимок
- **~100 байт / строка** в БД → 30 дней × 1 запись/мин ≈ **4.3 МБ**
- **Cleanup 10k строк: ~3 мс** (индекс на `timestamp` эффективен)

Production-оптимизация SQLite (конкурентность + скорость записи) — вызовите один
раз после создания engine:
```python
from app.integration.scheduler_setup import configure_sqlite_for_monitoring
configure_sqlite_for_monitoring(engine)   # WAL + synchronous=NORMAL + busy_timeout
```


## 🚀 План интеграции (для агента-интегратора)

> Все пути импорта в файлах написаны как `from app....` — **замените `app` на ваш
> корневой пакет** (или оставьте, если он так и называется).

### 1. Скопировать файлы в проект

- `models/system_metric.py`    → в ваш пакет моделей
- `services/monitor.py`        → в ваш пакет сервисов
- `admin/system_metric_admin.py` → в пакет админок
- `api/metrics_router.py`      → в пакет роутеров (если есть FastAPI)
- `integration/*.py`           → в пакет интеграции (или прямо в `__main__`)

### 2. Поставить зависимость

Добавить в `requirements.txt` строку:
```
psutil==6.1.0
```
(psutil поставляется готовым wheel для linux x86_64 — `gcc` не нужен)

### 3. Подключить модель к вашему Base

В `models/system_metric.py` **заменить заглушку**:
```python
# УДАЛИТЬ:
class Base(DeclarativeBase): ...
# РАСКОММЕНТИРОВАТЬ свой импорт:
from app.db.base import Base   # ← ваш реальный declarative base
```
И убедиться, что `SystemMetric` импортируется там же, где вызывается
`await conn.run_sync(Base.metadata.create_all)` (проще всего — добавить
`from app.models.system_metric import SystemMetric  # noqa: F401` в `models/__init__.py`).

### 4. SQLite на постоянном хранилище Amvera

URL БД должен указывать на `/data/...db` (иначе история потеряется при перезапуске):
```python
DATABASE_URL = "sqlite+aiosqlite:////data/app.db"   # 4 слэша — абсолютный путь /data/app.db
```
Локально для теста используйте `sqlite+aiosqlite:///./test.db`.

### 5. Создать Telegram-бота и узнать chat_id

1. Написать **@BotFather** → `/newbot` → получить **BOT_TOKEN**.
   (Если бот уже есть — переиспользуйте его.)
2. Создать группу/чат для алертов, добавить туда бота администратором.
3. Узнать **chat_id**:
   - переслать любое сообщение из чата боту **@userinfobot**, ИЛИ
   - открыть `https://api.telegram.org/bot<TOKEN>/getUpdates` и взять `chat.id`.
4. Проверить вручную:
   `https://api.telegram.org/bot<TOKEN>/sendMessage?chat_id=<ID>&text=test`

### 6. Задать переменные окружения (Amvera → Переменные/Секреты)

См. `.env.example`. Минимум:
```
MONITOR_ADMIN_CHAT_ID=<ваш chat_id>
MONITOR_SERVER_NAME=prod-amvera
MONITOR_DISK_PATH=/data
```
Остальное имеет адекватные значения по умолчанию (интервал 60с, пороги 90/85/90%,
ретенция 30 дней, кулдаун 300с).

### 7. Зарегистрировать мониторинг при старте

В точке инициализации (там, где создаётся `bot`, `scheduler`, `session_factory`):
```python
from app.integration.scheduler_setup import register_monitor_jobs

service = register_monitor_jobs(scheduler, bot=bot, session_factory=session_factory)
scheduler.start()
```
Либо через aiogram-хук — см. `integration/startup.py`.

### 8. Подключить sqladmin и FastAPI-роутер (по желанию)

```python
# sqladmin
from app.admin.system_metric_admin import SystemMetricAdmin
admin.add_view(SystemMetricAdmin)

# FastAPI (если есть веб-часть)
from app.api.metrics_router import router as metrics_router
app.include_router(metrics_router)
# В metrics_router.py замените get_session() на вашу зависимость сессии.
```

### 9. Конфиг Amvera

Скопировать `amvera.yml.example` → `amvera.yml` в корень репозитория,
поправить `meta.toolchain.version` (ваша версия Python), `run.scriptName`
(ваш entry-point) и `run.containerPort` (порт sqladmin/FastAPI).

## ⚠️ Замечания по платформе Amvera

- **Время сервера = UTC.** Метрики хранятся в UTC. В алертах тоже UTC.
- **Persistent storage монтируется в `/data`** (`run.persistenceMount: /data`).
  Диск мониторится именно по этому пути (`MONITOR_DISK_PATH=/data`).
- **psutil в контейнере** читает cgroup-метрики (CPU/RAM видны в рамках лимитов
  вашего тарифа, а не всего хоста) — это то, что нужно.
- **`os.getloadavg()` в контейнере** возвращает load average **хоста**, не контейнера.
  Если вводит в заблуждение — отключите: `MONITOR_COLLECT_LOAD=false`.
- **OOM-Killer**: K8s убьёт процесс при превышении лимита RAM. Поэтому
  `MONITOR_MEM_THRESHOLD` держите **ниже** лимита тарифа (напр. 85%, а не 95%).
- **pip-среда**: Amvera сама создаёт venv и ставит `requirements.txt`; Docker не нужен.

## ✅ Проверка после деплоя

1. В логах появится: `Monitor jobs registered: collect every 60s, cleanup at 03:00 UTC`.
2. Через ~1 минуту в БД появится запись `system_metrics` (видно в sqladmin).
3. `GET https://<ваш-проект>.amvera.io/api/metrics/latest` вернёт JSON.
4. Для проверки алерта временно поставьте `MONITOR_CPU_THRESHOLD=1` и понаблюдайте
   минуту — в Telegram должно прийти сообщение (с учётом кулдауна).

## 📊 Метрики и эндпоинты

| Эндпоинт | Что возвращает |
|---|---|
| `GET /api/metrics/latest` | последний снимок (виджет «сейчас») |
| `GET /api/metrics/history?hours=24` | массив точек для графика |
| `GET /api/metrics/summary?hours=24` | avg/max по CPU/RAM/Disk |

Поля снимка: `timestamp, cpu_percent, mem_percent, mem_used_mb, mem_total_mb,
disk_percent, disk_used_gb, disk_total_gb, net_bytes_sent, net_bytes_recv, load_avg_1`.

## 🔒 Безопасность

- `BOT_TOKEN`, `MONITOR_ADMIN_CHAT_ID`, `MONITOR_API_TOKEN` — только в **Amvera Secrets**, не в коде и не в репозитории.
- Алерты идут только в указанный `chat_id` (приватный чат/группа).
- **HTTP API `/api/metrics/*` защищён опциональным токеном `MONITOR_API_TOKEN`**:
  если переменная задана — каждый запрос обязан нести заголовок
  `X-Monitor-Token: <значение>` (сравнение за константное время через `secrets.compare_digest`,
  защита от timing-атаки). **Для публичного URL Amvera задайте токен обязательно** —
  иначе метрики ресурсов будут доступны всем. Сгенерировать:
  `python -c "import secrets; print(secrets.token_urlsafe(32))"`.
- Ответ `/history` ограничен `MAX_HISTORY_POINTS` (5000) — защита от тяжёлого ответа (DoS);
  `hours` ограничен `1..720`, прочие параметры валидируются FastAPI (422 при нарушении).
- Внешних HTTP-вызовов нет (только Telegram API для алертов). Никаких `subprocess`/`eval`/`exec`.
- Все SQL-запросы — через ORM SQLAlchemy с параметризацией (инъекций нет).
- Ошибки логируются (`logger.exception`), но клиентам отдаются только коды 401/404/422 —
  без стек-трейсов и секретов.
- Дополнительно (на ваше усмотрение): CORS, если дашборд и API на разных доменах;
  права `chmod 600` на файл SQLite на `/data`.

## 🔧 Тонкая настройка

| Переменная | По умолчанию | Описание |
|---|---|---|
| `MONITOR_INTERVAL_SEC` | 60 | частота сбора метрик |
| `MONITOR_RETENTION_DAYS` | 30 | сколько дней хранить историю |
| `MONITOR_CPU_THRESHOLD` | 90 | % CPU для алерта |
| `MONITOR_MEM_THRESHOLD` | 85 | % RAM для алерта |
| `MONITOR_DISK_THRESHOLD` | 90 | % Disk для алерта |
| `MONITOR_ALERT_COOLDOWN_SEC` | 300 | кулдаун однотипных алертов |
| `MONITOR_DISK_PATH` | /data | путь постоянного хранилища |
| `MONITOR_COLLECT_LOAD` | true | собирать ли load average |
| `MONITOR_API_TOKEN` | (пусто) | токен доступа к `/api/metrics/*` (задайте для публичного URL) |

---
Лицензия модуля: используйте свободно в рамках вашего проекта.