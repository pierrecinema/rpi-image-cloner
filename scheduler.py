from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from datetime import datetime
from database import get_db
from ssh_backup import run_backup

scheduler = AsyncIOScheduler()


def init_scheduler():
    scheduler.start()
    _load_jobs()


def _load_jobs():
    scheduler.remove_all_jobs()
    conn = get_db()
    pis = conn.execute(
        "SELECT id, name, schedule_cron, safe_mode FROM pis WHERE schedule_enabled=1"
    ).fetchall()
    conn.close()
    for pi in pis:
        _add_job(pi['id'], pi['name'], pi['schedule_cron'], bool(pi['safe_mode']))


def _add_job(pi_id, name, cron_expr, safe_mode):
    try:
        if cron_expr and cron_expr.startswith('once:'):
            dt = datetime.fromisoformat(cron_expr[5:])
            trigger = DateTrigger(run_date=dt)
        else:
            trigger = CronTrigger.from_crontab(cron_expr)
        scheduler.add_job(
            run_backup,
            trigger,
            id=f'backup_{pi_id}',
            args=[pi_id, safe_mode],
            replace_existing=True,
            name=f'Backup {name}',
        )
    except Exception as e:
        print(f"[Scheduler] Fehler beim Hinzufügen von Job für Pi {pi_id}: {e}")


def update_job(pi_id, name, cron_expr, safe_mode, enabled):
    job_id = f'backup_{pi_id}'
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)
    if enabled:
        _add_job(pi_id, name, cron_expr, safe_mode)


def remove_job(pi_id):
    job_id = f'backup_{pi_id}'
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)


def get_next_run(pi_id) -> str | None:
    job = scheduler.get_job(f'backup_{pi_id}')
    if job and job.next_run_time:
        return job.next_run_time.strftime("%d.%m.%Y %H:%M")
    return None