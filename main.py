import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from fastapi.requests import Request
from pydantic import BaseModel

from database import init_db, get_db, encrypt_password, decrypt_password
from ssh_backup import run_backup, run_verify, get_sizes, open_image_stream, active_backups, active_verifies
from scheduler import init_scheduler, update_job, remove_job, get_next_run, scheduler

templates = Jinja2Templates(directory="templates")


@asynccontextmanager
async def lifespan(app):
    init_db()
    init_scheduler()
    yield
    scheduler.shutdown()


app = FastAPI(title="Raspberry Pi Image Cloner", redirect_slashes=False, lifespan=lifespan)


# --- Models ---

class PiCreate(BaseModel):
    name: str
    ip: str
    port: int = 22
    user: str
    password: str
    backup_device: str = '/dev/sda'
    excludes: str = ''
    schedule_enabled: bool = False
    schedule_cron: str = '0 3 * * *'
    safe_mode: bool = False


class PiUpdate(BaseModel):
    name: str
    ip: str
    port: int = 22
    user: str
    password: Optional[str] = None
    backup_device: str = '/dev/sda'
    excludes: str = ''
    schedule_enabled: bool = False
    schedule_cron: str = '0 3 * * *'
    safe_mode: bool = False


# --- Routes ---

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/api/pis")
async def get_pis():
    conn = get_db()
    pis = conn.execute(
        "SELECT id, name, ip, port, user, backup_device, excludes, "
        "schedule_enabled, schedule_cron, safe_mode FROM pis"
    ).fetchall()
    result = []
    for pi in pis:
        d = dict(pi)
        last = conn.execute(
            "SELECT started_at, status, duration_seconds FROM backups "
            "WHERE pi_id=? ORDER BY started_at DESC LIMIT 1",
            (pi['id'],)
        ).fetchone()
        d['last_backup'] = dict(last) if last else None
        d['is_running'] = pi['id'] in active_backups
        d['next_run'] = get_next_run(pi['id'])
        result.append(d)
    conn.close()
    return result


@app.post("/api/pis")
async def create_pi(pi: PiCreate):
    if not pi.password:
        raise HTTPException(400, "Passwort erforderlich")
    conn = get_db()
    cursor = conn.execute(
        "INSERT INTO pis (name, ip, port, user, password, backup_device, excludes, "
        "schedule_enabled, schedule_cron, safe_mode) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (pi.name, pi.ip, pi.port, pi.user, encrypt_password(pi.password),
         pi.backup_device, pi.excludes, int(pi.schedule_enabled), pi.schedule_cron, int(pi.safe_mode))
    )
    pi_id = cursor.lastrowid
    conn.commit()
    conn.close()
    update_job(pi_id, pi.name, pi.schedule_cron, pi.safe_mode, pi.schedule_enabled)
    return {"id": pi_id}


@app.put("/api/pis/{pi_id}")
async def update_pi(pi_id: int, pi: PiUpdate):
    conn = get_db()
    existing = conn.execute("SELECT password FROM pis WHERE id=?", (pi_id,)).fetchone()
    if not existing:
        raise HTTPException(404, "Pi nicht gefunden")
    new_pw = encrypt_password(pi.password) if pi.password else existing['password']
    conn.execute(
        "UPDATE pis SET name=?, ip=?, port=?, user=?, password=?, backup_device=?, "
        "excludes=?, schedule_enabled=?, schedule_cron=?, safe_mode=? WHERE id=?",
        (pi.name, pi.ip, pi.port, pi.user, new_pw,
         pi.backup_device, pi.excludes, int(pi.schedule_enabled), pi.schedule_cron, int(pi.safe_mode), pi_id)
    )
    conn.commit()
    conn.close()
    update_job(pi_id, pi.name, pi.schedule_cron, pi.safe_mode, pi.schedule_enabled)
    return {"ok": True}


@app.delete("/api/pis/{pi_id}")
async def delete_pi(pi_id: int):
    remove_job(pi_id)
    conn = get_db()
    conn.execute("DELETE FROM pis WHERE id=?", (pi_id,))
    conn.execute("DELETE FROM backups WHERE pi_id=?", (pi_id,))
    conn.commit()
    conn.close()
    return {"ok": True}


@app.post("/api/pis/{pi_id}/backup")
async def start_backup(pi_id: int, safe_mode: bool = False):
    if pi_id in active_backups:
        raise HTTPException(400, "Backup läuft bereits")
    ok, result = run_backup(pi_id, safe_mode)
    if not ok:
        raise HTTPException(400, result)
    return {"backup_id": result}


@app.get("/api/pis/{pi_id}/backup/stream")
async def stream_backup(pi_id: int):
    async def generate():
        if pi_id not in active_backups:
            conn = get_db()
            last = conn.execute(
                "SELECT log FROM backups WHERE pi_id=? ORDER BY started_at DESC LIMIT 1",
                (pi_id,)
            ).fetchone()
            conn.close()
            if last:
                for line in (last['log'] or '').split('\n'):
                    yield f"data: {json.dumps({'line': line})}\n\n"
            yield f"data: {json.dumps({'done': True})}\n\n"
            return

        q = active_backups[pi_id]['queue']
        loop = asyncio.get_event_loop()
        while True:
            try:
                msg = await loop.run_in_executor(None, lambda: q.get(timeout=1))
                if msg is None:
                    yield f"data: {json.dumps({'done': True})}\n\n"
                    return
                yield f"data: {json.dumps({'line': msg})}\n\n"
            except Exception:
                if pi_id not in active_backups:
                    yield f"data: {json.dumps({'done': True})}\n\n"
                    return
                yield f"data: {json.dumps({'ping': True})}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/pis/{pi_id}/verify/start")
async def start_verify(pi_id: int):
    q = run_verify(pi_id)
    if q is None:
        raise HTTPException(404, "Pi nicht gefunden")
    active_verifies[pi_id] = q
    return {"ok": True}


@app.get("/api/pis/{pi_id}/verify/stream")
async def stream_verify(pi_id: int):
    async def generate():
        if pi_id not in active_verifies:
            yield f"data: {json.dumps({'line': 'Keine aktive Verifikation. Bitte zuerst starten.'})}\n\n"
            yield f"data: {json.dumps({'done': True})}\n\n"
            return

        q = active_verifies[pi_id]
        loop = asyncio.get_event_loop()
        while True:
            try:
                msg = await loop.run_in_executor(None, lambda: q.get(timeout=1))
                if msg is None:
                    yield f"data: {json.dumps({'done': True})}\n\n"
                    active_verifies.pop(pi_id, None)
                    return
                yield f"data: {json.dumps({'line': msg})}\n\n"
            except Exception:
                if pi_id not in active_verifies:
                    yield f"data: {json.dumps({'done': True})}\n\n"
                    return
                yield f"data: {json.dumps({'ping': True})}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/pis/{pi_id}/sizes")
async def pi_sizes(pi_id: int):
    result = get_sizes(pi_id)
    if result is None:
        raise HTTPException(404, "Pi nicht gefunden")
    return result


@app.get("/api/pis/{pi_id}/backups")
async def backup_history(pi_id: int):
    conn = get_db()
    rows = conn.execute(
        "SELECT id, started_at, finished_at, status, duration_seconds FROM backups "
        "WHERE pi_id=? ORDER BY started_at DESC LIMIT 20",
        (pi_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.get("/api/pis/{pi_id}/image")
async def download_image(pi_id: int):
    if pi_id in active_backups:
        raise HTTPException(400, "Backup läuft gerade – bitte erst warten bis es abgeschlossen ist")

    loop = asyncio.get_event_loop()
    try:
        ssh, chan, filename = await loop.run_in_executor(None, lambda: open_image_stream(pi_id))
    except ValueError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        raise HTTPException(500, f"Verbindungsfehler: {e}")

    async def chunks():
        try:
            while True:
                chunk = await loop.run_in_executor(None, lambda: chan.recv(65536))
                if not chunk:
                    break
                yield chunk
        finally:
            try:
                chan.close()
                ssh.close()
            except Exception:
                pass

    return StreamingResponse(
        chunks(),
        media_type="application/gzip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )