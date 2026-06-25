"""Pytest bootstrap: делает monitoring-module доступным как пакет `app`."""
from __future__ import annotations

import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Позволяет использовать импорты `from app.services.monitor import ...`
# без изменения структуры проекта в локальных тестах.
_app = types.ModuleType("app")
_app.__path__ = [ROOT]
sys.modules["app"] = _app
