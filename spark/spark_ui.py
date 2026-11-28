"""
Крок 7: вебінтерфейс для показу результатів Spark map-reduce.

Невеликий Flask-застосунок, який працює всередині контейнера spark:
    GET  /                  - сторінка з кнопкою запуску й таблицями;
    POST /api/spark/run     - запускає spark_job.py у фоновому потоці;
    GET  /api/spark/status  - стан завдання: idle / running / done / error;
    GET  /api/spark/result  - вміст results/spark_result.json.

Запуск (усередині контейнера spark):
    python /app/spark_ui.py
Відкрити: http://127.0.0.1:8767/
"""

import json
import os
import subprocess
import sys
import threading
import time

from flask import Flask, jsonify, render_template

# ============================== НАЛАШТУВАННЯ ==============================
APP_DIR = os.path.dirname(os.path.abspath(__file__))
JOB_PATH = os.path.join(APP_DIR, "spark_job.py")
RESULT_PATH = os.path.join(APP_DIR, "results", "spark_result.json")
JOB_TIMEOUT_SEC = 600
HOST = "0.0.0.0"   # інакше порт не буде доступний ззовні контейнера
PORT = 8767
# ==========================================================================

app = Flask(__name__)

# Стан завдання спільний для всіх запитів; змінюється лише під замком.
_lock = threading.Lock()
_state = {"status": "idle", "started_at": None, "finished_at": None, "error": None}


def _run_job() -> None:
    """Виконує spark_job.py як окремий процес і оновлює стан."""
    try:
        proc = subprocess.run(
            [sys.executable, JOB_PATH],
            cwd=APP_DIR,
            capture_output=True,
            text=True,
            timeout=JOB_TIMEOUT_SEC,
        )
        with _lock:
            if proc.returncode == 0:
                _state.update(status="done", error=None)
            else:
                # Для діагностики показуємо лише кінець виводу помилки.
                _state.update(status="error", error=(proc.stderr or proc.stdout)[-2000:])
    except subprocess.TimeoutExpired:
        with _lock:
            _state.update(status="error", error=f"Перевищено ліміт часу {JOB_TIMEOUT_SEC} с")
    except Exception as exc:  # непередбачена помилка запуску
        with _lock:
            _state.update(status="error", error=str(exc))
    finally:
        with _lock:
            _state["finished_at"] = time.time()


@app.get("/")
def index():
    return render_template("spark.html")


@app.post("/api/spark/run")
def run_job():
    """Запускає завдання, якщо воно ще не виконується."""
    with _lock:
        if _state["status"] == "running":
            return jsonify({"status": "running", "message": "Завдання вже виконується"}), 409
        _state.update(status="running", started_at=time.time(), finished_at=None, error=None)
    threading.Thread(target=_run_job, daemon=True).start()
    return jsonify({"status": "running"}), 202


@app.get("/api/spark/status")
def get_status():
    with _lock:
        snapshot = dict(_state)
    snapshot["has_result"] = os.path.exists(RESULT_PATH)
    snapshot["now"] = time.time()
    return jsonify(snapshot)


@app.get("/api/spark/result")
def get_result():
    if not os.path.exists(RESULT_PATH):
        return jsonify({"error": "Результату ще немає. Запустіть запит."}), 404
    try:
        with open(RESULT_PATH, encoding="utf-8") as f:
            return jsonify(json.load(f))
    except (OSError, json.JSONDecodeError):
        return jsonify({"error": "Файл результату недоступний або пошкоджений."}), 503


if __name__ == "__main__":
    app.run(host=HOST, port=PORT, threaded=True)
