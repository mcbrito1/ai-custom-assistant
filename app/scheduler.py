import json
import os
import uuid
import logging
from datetime import datetime, timezone
from pathlib import Path
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

log = logging.getLogger(__name__)

TASKS_FILE = os.getenv("TASKS_FILE", "/app/data/tasks.json")

# Callback set by main.py to send Telegram messages
_notify_callback = None
_scheduler = BackgroundScheduler(timezone="America/Sao_Paulo")


def set_notify_callback(fn):
    global _notify_callback
    _notify_callback = fn


def _fire_task(task_id: str, message: str, chat_id: str):
    log.info(f"Firing task {task_id}: {message}")
    if _notify_callback:
        _notify_callback(chat_id=chat_id, text=f"⏰ *Lembrete:* {message}", task_id=task_id)


# ── Persistence ──────────────────────────────────────────────────────────────

def _load_tasks() -> dict:
    path = Path(TASKS_FILE)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_tasks(tasks: dict):
    path = Path(TASKS_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(tasks, ensure_ascii=False, indent=2), encoding="utf-8")


# ── Public API ────────────────────────────────────────────────────────────────

def add_once(message: str, chat_id: str, run_at: datetime) -> str:
    """Schedule a one-time reminder."""
    task_id = str(uuid.uuid4())[:8]
    _scheduler.add_job(
        _fire_task,
        trigger=DateTrigger(run_date=run_at),
        kwargs={"task_id": task_id, "message": message, "chat_id": chat_id},
        id=task_id,
        misfire_grace_time=300,
    )
    tasks = _load_tasks()
    tasks[task_id] = {
        "id": task_id,
        "type": "once",
        "message": message,
        "chat_id": chat_id,
        "run_at": run_at.isoformat(),
        "created_at": datetime.now().isoformat(),
    }
    _save_tasks(tasks)
    return task_id


def add_recurring(message: str, chat_id: str, cron_expr: str) -> str:
    """Schedule a recurring reminder using a cron expression."""
    task_id = str(uuid.uuid4())[:8]
    trigger = CronTrigger.from_crontab(cron_expr, timezone="America/Sao_Paulo")
    _scheduler.add_job(
        _fire_task,
        trigger=trigger,
        kwargs={"task_id": task_id, "message": message, "chat_id": chat_id},
        id=task_id,
        misfire_grace_time=300,
    )
    tasks = _load_tasks()
    tasks[task_id] = {
        "id": task_id,
        "type": "recurring",
        "message": message,
        "chat_id": chat_id,
        "cron": cron_expr,
        "created_at": datetime.now().isoformat(),
    }
    _save_tasks(tasks)
    return task_id


def cancel_task(task_id: str) -> bool:
    tasks = _load_tasks()
    if task_id not in tasks:
        return False
    try:
        _scheduler.remove_job(task_id)
    except Exception:
        pass
    del tasks[task_id]
    _save_tasks(tasks)
    return True


def list_tasks() -> list[dict]:
    return list(_load_tasks().values())


def remove_completed(task_id: str):
    """Called after a one-time task fires to clean up persistence."""
    tasks = _load_tasks()
    if task_id in tasks and tasks[task_id].get("type") == "once":
        del tasks[task_id]
        _save_tasks(tasks)


def add_internal_cron(fn, cron_expr: str, job_id: str):
    """Add a non-persistent internal cron job (system use only)."""
    try:
        trigger = CronTrigger.from_crontab(cron_expr, timezone="America/Sao_Paulo")
        _scheduler.add_job(fn, trigger=trigger, id=job_id, replace_existing=True)
        log.info(f"Internal cron job '{job_id}' scheduled: {cron_expr}")
    except Exception as e:
        log.warning(f"Failed to add internal job '{job_id}': {e}")


def start():
    """Start scheduler and reload persisted tasks."""
    tasks = _load_tasks()
    now = datetime.now(tz=timezone.utc)

    for task in list(tasks.values()):
        try:
            if task["type"] == "once":
                run_at = datetime.fromisoformat(task["run_at"])
                if run_at.tzinfo is None:
                    from dateutil import tz as dtz
                    run_at = run_at.replace(tzinfo=dtz.gettz("America/Sao_Paulo"))
                if run_at.astimezone(timezone.utc) <= now:
                    # Missed — remove stale task
                    del tasks[task["id"]]
                    continue
                _scheduler.add_job(
                    _fire_task,
                    trigger=DateTrigger(run_date=run_at),
                    kwargs={"task_id": task["id"], "message": task["message"], "chat_id": task["chat_id"]},
                    id=task["id"],
                    misfire_grace_time=300,
                )
            elif task["type"] == "recurring":
                trigger = CronTrigger.from_crontab(task["cron"], timezone="America/Sao_Paulo")
                _scheduler.add_job(
                    _fire_task,
                    trigger=trigger,
                    kwargs={"task_id": task["id"], "message": task["message"], "chat_id": task["chat_id"]},
                    id=task["id"],
                    misfire_grace_time=300,
                )
        except Exception as e:
            log.warning(f"Failed to restore task {task.get('id')}: {e}")

    _save_tasks(tasks)
    _scheduler.start()
    log.info(f"Scheduler started with {len(_scheduler.get_jobs())} jobs.")
