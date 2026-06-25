# Amvera Server Monitoring Module

[![Use this template](https://img.shields.io/badge/-Use%20this%20template-brightgreen)](https://github.com/BOSSincrypto/amvera-monitoring-module/generate)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![CI](https://github.com/BOSSincrypto/amvera-monitoring-module/actions/workflows/ci.yml/badge.svg)](https://github.com/BOSSincrypto/amvera-monitoring-module/actions)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

**Lightweight, zero-cost container monitoring for Amvera-hosted Python apps.**

Collect CPU, RAM, disk (`/data`), network and load-average metrics every minute, store them in SQLite via SQLAlchemy, send Telegram alerts via aiogram, and expose JSON endpoints through FastAPI for your dashboard — all with **+0 extra processes**, **+1 dependency (`psutil`)**, and **~2–3 MB RAM**.

> 🇷🇺 Russian version: [`README.ru.md`](README.ru.md)

## Why this module

- **Made for Amvera pip environments** — no Docker, no extra containers.
- **aiogram 3 native** — alerts are sent with your existing bot.
- **SQLAlchemy 2.0 async + aiosqlite** — tiny footprint, durable history.
- **FastAPI metrics API** — ready for charts and widgets.
- **sqladmin view** — inspect raw metrics in the admin panel.
- **Security-minded** — token-protected endpoints, constant-time token comparison, HTML-escaped alerts.

## Stack

- aiogram 3
- SQLAlchemy 2.0 async + aiosqlite
- APScheduler (AsyncIOScheduler)
- sqladmin
- FastAPI
- psutil

## Quick start

```bash
# 1. Copy the module into your project
#    (replace `app` in imports with your actual root package name)
cp -r admin api integration models services tests /path/to/your/app/

# 2. Add the only new production dependency
pip install psutil

# 3. Import the model so SQLAlchemy creates the table
from app.models.system_metric import SystemMetric  # noqa: F401

# 4. Register jobs where your scheduler is initialized
from app.integration.scheduler_setup import register_monitor_jobs

service = register_monitor_jobs(scheduler, bot=bot, session_factory=session_factory)
scheduler.start()
```

See [`README.ru.md`](README.ru.md) for the full integration guide, or the sample `integration/startup.py` for an aiogram bot.

## Configuration

Set environment variables in Amvera **Secrets/Variables**:

```bash
MONITOR_ADMIN_CHAT_ID=123456789        # Telegram chat id for alerts
MONITOR_SERVER_NAME=prod-amvera        # Server label shown in alerts
MONITOR_INTERVAL_SEC=60                # Collection interval (min 15)
MONITOR_RETENTION_DAYS=30              # How many days to keep history
MONITOR_CPU_THRESHOLD=90
MONITOR_MEM_THRESHOLD=85
MONITOR_DISK_THRESHOLD=90
MONITOR_DISK_PATH=/data
MONITOR_ALERT_COOLDOWN_SEC=300
MONITOR_COLLECT_LOAD=true
MONITOR_API_TOKEN=                     # Set a strong token for public Amvera URLs
```

Generate a token:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

## HTTP API

| Endpoint | Description |
|---|---|
| `GET /api/metrics/latest` | Latest snapshot (current state widget) |
| `GET /api/metrics/history?hours=24` | Points for line charts, capped at 5000 |
| `GET /api/metrics/summary?hours=24` | avg/max for CPU/RAM/Disk |

If `MONITOR_API_TOKEN` is set, every request must include:

```http
X-Monitor-Token: <your-token>
```

## Security highlights

- Token comparison uses `secrets.compare_digest` to resist timing attacks.
- Alerts are sent with `parse_mode="HTML"` and `server_name` is escaped.
- No `subprocess`, `eval`, or `exec`.
- SQL queries are ORM-parameterized.
- `hours` and `MAX_HISTORY_POINTS` are bounded to prevent large responses.

## Development & testing

```bash
cd amvera-monitoring-module
python -m venv .venv
source .venv/bin/activate  # or .venv/Scripts/activate on Windows
pip install -r requirements-dev.txt

pytest -q                  # 24 tests, ~95% coverage
ruff check .
mypy .
bandit -r . -c pyproject.toml
python tests/test_perf.py  # profiling
```

## Performance

Measured on a local machine:

- `collect_and_store`: **~7 ms / call**
- Python allocations per snapshot: **~0.18 MB**
- Database after 10 000 rows: **~1 MB**
- Cleanup 10k old rows: **~3 ms**

## License

MIT — see [`LICENSE`](LICENSE).

## Keywords / tags

`amvera`, `monitoring`, `aiogram`, `telegram-bot`, `fastapi`, `sqlalchemy`, `psutil`, `container-monitoring`, `server-metrics`, `alerts`, `python-monitoring`, `sqlite`
