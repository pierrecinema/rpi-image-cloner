import paramiko
import threading
import queue
from datetime import datetime
from database import get_db, decrypt_password

# pi_id -> {queue, backup_id, status, started_at, thread}
active_backups: dict = {}

# pi_id -> queue (for verify streams)
active_verifies: dict = {}


def run_backup(pi_id: int, safe_mode: bool = False):
    if pi_id in active_backups:
        return False, "Backup läuft bereits"

    conn = get_db()
    pi = conn.execute("SELECT * FROM pis WHERE id=?", (pi_id,)).fetchone()
    conn.close()
    if not pi:
        return False, "Pi nicht gefunden"

    conn = get_db()
    cursor = conn.execute(
        "INSERT INTO backups (pi_id, started_at, status) VALUES (?, ?, 'running')",
        (pi_id, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    )
    backup_id = cursor.lastrowid
    conn.commit()
    conn.close()

    log_queue = queue.Queue()
    active_backups[pi_id] = {
        'queue': log_queue,
        'backup_id': backup_id,
        'status': 'running',
        'started_at': datetime.now(),
    }

    t = threading.Thread(target=_backup_worker, args=(pi_id, backup_id, dict(pi), safe_mode, log_queue), daemon=True)
    t.start()
    active_backups[pi_id]['thread'] = t
    return True, backup_id


def _progress_monitor(pi, password, log_queue, stop_event, started_at):
    """Separate SSH connection to monitor backup progress every 15s."""
    try:
        ssh2 = _connect(pi, password)
    except Exception:
        return

    # Total USED space on source — read once upfront
    try:
        raw = _exec_out(ssh2, password, "df / | tail -1 | awk '{print $3}'").strip()
        source_used_kb = int(raw)
    except Exception:
        ssh2.close()
        return

    def fmt_kb(kb):
        if kb >= 1024 * 1024:
            return f"{kb / 1024 / 1024:.1f} GB"
        if kb >= 1024:
            return f"{kb / 1024:.0f} MB"
        return f"{kb} KB"

    # Detect device number of / to distinguish from /mnt/clone
    try:
        root_dev = _exec_out(ssh2, password,
                             "stat -c %d / 2>/dev/null").strip()
    except Exception:
        root_dev = ""

    while not stop_event.is_set():
        stop_event.wait(15)
        if stop_event.is_set():
            break
        try:
            elapsed = (datetime.now() - started_at).total_seconds()
            elapsed_m = int(elapsed // 60)
            elapsed_s = int(elapsed % 60)

            # Check /mnt/clone is a DIFFERENT filesystem than /
            clone_dev = _exec_out(ssh2, password,
                                  "stat -c %d /mnt/clone 2>/dev/null").strip()
            if not clone_dev or clone_dev == root_dev:
                log_queue.put(
                    f"[FORTSCHRITT] Laufzeit: {elapsed_m}:{elapsed_s:02d} — Vorbereitung..."
                )
                continue

            # Used space on clone partition
            clone_raw = _exec_out(ssh2, password,
                                  "df /mnt/clone | tail -1 | awk '{print $3}'").strip()
            if not clone_raw or not clone_raw.isdigit():
                continue
            clone_used_kb = int(clone_raw)

            pct = min(99, int(clone_used_kb / source_used_kb * 100)) if source_used_kb > 0 else 0

            eta_str = "—"
            if pct > 5:
                remaining_pct = 100 - pct
                if pct < 99:
                    rate = pct / elapsed  # pct per second
                    remaining_secs = remaining_pct / rate if rate > 0 else 0
                    eta_str = f"{int(remaining_secs // 60)} Min {int(remaining_secs % 60)} Sek"

            bar_filled = pct // 5
            bar = "█" * bar_filled + "░" * (20 - bar_filled)

            line = (
                f"[FORTSCHRITT] {pct}% ▕{bar}▏ "
                f"{fmt_kb(clone_used_kb)} / {fmt_kb(source_used_kb)} — "
                f"Laufzeit: {elapsed_m}:{elapsed_s:02d} — ETA: {eta_str}"
            )
            log_queue.put(line)
        except Exception:
            pass

    try:
        ssh2.close()
    except Exception:
        pass


def _fix_boot(ssh, password, device, log):
    """Post-backup fix: ensure boot partition has files and cmdline.txt has correct PARTUUID."""
    try:
        part1 = _part(device, 1)
        part2 = _part(device, 2)

        partuuid = _exec_out(ssh, password,
            f"blkid {part2} | grep -o 'PARTUUID=\"[^\"]*\"' | cut -d'\"' -f2").strip()
        if not partuuid:
            log(f"[POST-FIX] PARTUUID von {part2} nicht gefunden — übersprungen")
            return

        # Let USB device finish any pending writes from rpi-clone
        _exec_out(ssh, password, f"sync; sleep 3; blockdev --flushbufs {device} 2>/dev/null; true")

        _exec_out(ssh, password, "mkdir -p /tmp/_bootfix")
        _exec_out(ssh, password, f"umount /tmp/_bootfix 2>/dev/null; true")
        _exec_out(ssh, password, f"mount {part1} /tmp/_bootfix")

        used_kb = _exec_out(ssh, password,
            "df /tmp/_bootfix | tail -1 | awk '{print $3}'").strip()
        boot_empty = int(used_kb) < 1024 if used_kb.isdigit() else True

        if boot_empty:
            # Source is always the running Pi's boot partition (mmcblk0p1)
            src = _exec_out(ssh, password,
                "lsblk -no PATH,MOUNTPOINT | awk '$2==\"/boot/firmware\"{print $1}'").strip() or "/dev/mmcblk0p1"
            log(f"[POST-FIX] Boot-Partition leer — dd {src} → {part1}...")
            _exec_out(ssh, password, f"umount /tmp/_bootfix 2>/dev/null; true")
            _exec_out(ssh, password, f"dd if={src} of={part1} bs=4M")
            _exec_out(ssh, password, f"sync; sleep 2; blockdev --flushbufs {device} 2>/dev/null; true")
            _exec_out(ssh, password, f"mount {part1} /tmp/_bootfix")
            log(f"[POST-FIX] Boot-Dateien kopiert")

        # Fix PARTUUID in cmdline.txt
        current = _exec_out(ssh, password, "cat /tmp/_bootfix/cmdline.txt 2>/dev/null").strip()
        if f"root=PARTUUID={partuuid}" in current:
            log(f"[POST-FIX] cmdline.txt bereits korrekt (PARTUUID={partuuid})")
        else:
            _exec_out(ssh, password,
                f"sed -i 's/root=PARTUUID=[^ ]*/root=PARTUUID={partuuid}/' /tmp/_bootfix/cmdline.txt")
            log(f"[POST-FIX] cmdline.txt korrigiert → root=PARTUUID={partuuid}")

        # Fix fstab on root partition (part2)
        _exec_out(ssh, password, "mkdir -p /tmp/_rootfix")
        _exec_out(ssh, password, f"umount /tmp/_rootfix 2>/dev/null; true")
        _exec_out(ssh, password, f"mount {part2} /tmp/_rootfix")
        part1_uuid = _exec_out(ssh, password,
            f"blkid {part1} | grep -o 'PARTUUID=\"[^\"]*\"' | cut -d'\"' -f2").strip()
        fstab_content = _exec_out(ssh, password, "cat /tmp/_rootfix/etc/fstab 2>/dev/null").strip()
        if part1_uuid and part1_uuid in fstab_content and partuuid in fstab_content:
            log(f"[POST-FIX] fstab bereits korrekt")
        else:
            _exec_out(ssh, password,
                f"sed -i 's/PARTUUID=[a-f0-9-]*/PARTUUID={part1_uuid}/1' /tmp/_rootfix/etc/fstab")
            _exec_out(ssh, password,
                f"sed -i 's/PARTUUID=[a-f0-9-]*/PARTUUID={partuuid}/2' /tmp/_rootfix/etc/fstab")
            log(f"[POST-FIX] fstab korrigiert → {part1_uuid} / {partuuid}")
        _exec_out(ssh, password, f"umount /tmp/_rootfix 2>/dev/null; true")

        _exec_out(ssh, password, "sync")
        _exec_out(ssh, password, "umount /tmp/_bootfix 2>/dev/null; true")
    except Exception as e:
        log(f"[POST-FIX] Fehler: {e}")
        try:
            _exec_out(ssh, password, "sync; umount /tmp/_bootfix 2>/dev/null; true")
        except Exception:
            pass


def _backup_worker(pi_id, backup_id, pi, safe_mode, log_queue):
    password = decrypt_password(pi['password'])

    def log(msg):
        log_queue.put(msg)
        conn = get_db()
        conn.execute("UPDATE backups SET log = log || ? WHERE id=?", (msg + '\n', backup_id))
        conn.commit()
        conn.close()

    started_at = active_backups[pi_id]['started_at']
    stop_monitor = threading.Event()
    monitor_thread = threading.Thread(
        target=_progress_monitor,
        args=(pi, password, log_queue, stop_monitor, started_at),
        daemon=True
    )
    monitor_thread.start()

    status = 'error'
    try:
        ssh = _connect(pi, password)
        log(f"[{_ts()}] Verbunden mit {pi['ip']}")

        if safe_mode:
            log(f"[{_ts()}] Stoppe Docker Container...")
            _exec(ssh, password, "docker stop $(docker ps -q) 2>/dev/null; true", log)

        excludes = pi.get('excludes') or ''
        exclude_args = ''
        for ex in excludes.splitlines():
            ex = ex.strip()
            if ex:
                exclude_args += f' --exclude="{ex}"'

        device = pi.get('backup_device') or '/dev/sda'
        _exec(ssh, password, 'mountpoint -q /boot/firmware || mount /boot/firmware 2>/dev/null; true', lambda x: None)
        cmd = f'ionice -c3 nice -n 19 sudo -S rpi-clone {device} -U -f{exclude_args} 2>&1'

        log(f"[{_ts()}] Starte rpi-clone → {device}")
        log("=" * 50)

        chan = ssh.get_transport().open_session()
        chan.get_pty()
        chan.exec_command(f'echo {_sq(password)} | {cmd}')
        active_backups[pi_id]['chan'] = chan

        buf = b''
        clone_output = []
        while True:
            try:
                chunk = chan.recv(4096)
            except Exception:
                chunk = b''
            if not chunk:
                break
            buf += chunk
            while b'\n' in buf:
                line, buf = buf.split(b'\n', 1)
                decoded = line.decode('utf-8', errors='replace').rstrip()
                clone_output.append(decoded)
                log(decoded)

        if buf:
            decoded = buf.decode('utf-8', errors='replace').rstrip()
            clone_output.append(decoded)
            log(decoded)

        cancelled = active_backups.get(pi_id, {}).get('cancel', False)

        if cancelled:
            try:
                ssh_kill = _connect(pi, password)
                _exec_out(ssh_kill, password, "sudo pkill -f rpi-clone 2>/dev/null; true")
                ssh_kill.close()
            except Exception:
                pass
            log(f"[{_ts()}] Backup abgebrochen")
            status = 'cancelled'
        else:
            exit_code = chan.recv_exit_status()
            clone_aborted = any('Aborting' in l or 'could not unmount' in l for l in clone_output)

            if safe_mode:
                log(f"[{_ts()}] Starte Docker Container...")
                _exec(ssh, password, "docker start $(docker ps -a -q) 2>/dev/null; true", log)

            if exit_code == 0 and not clone_aborted:
                _fix_boot(ssh, password, device, log)

            status = 'success' if (exit_code == 0 and not clone_aborted) else 'error'
            result_msg = 'erfolgreich abgeschlossen' if exit_code == 0 else f'fehlgeschlagen (Exit {exit_code})'
            log(f"[{_ts()}] Backup {result_msg}")
            if exit_code == 0:
                log_queue.put("[FORTSCHRITT] 100% ▕████████████████████▏ Backup abgeschlossen")

        ssh.close()

    except Exception as e:
        log(f"[FEHLER] {e}")
    finally:
        stop_monitor.set()
        monitor_thread.join(timeout=5)
        started = active_backups[pi_id]['started_at']
        duration = int((datetime.now() - started).total_seconds())
        conn = get_db()
        conn.execute(
            "UPDATE backups SET status=?, finished_at=?, duration_seconds=? WHERE id=?",
            (status, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), duration, backup_id)
        )
        conn.commit()
        conn.close()
        active_backups[pi_id]['status'] = status
        log_queue.put(None)
        active_backups.pop(pi_id, None)


def run_verify(pi_id: int):
    conn = get_db()
    pi = conn.execute("SELECT * FROM pis WHERE id=?", (pi_id,)).fetchone()
    conn.close()
    if not pi:
        return None

    log_queue = queue.Queue()
    active_verifies[pi_id] = log_queue

    t = threading.Thread(target=_verify_worker, args=(dict(pi), log_queue), daemon=True)
    t.start()
    return log_queue


def _verify_worker(pi, log_queue):
    password = decrypt_password(pi['password'])
    device = pi.get('backup_device') or '/dev/sda'

    def log(msg):
        log_queue.put(msg)

    try:
        ssh = _connect(pi, password)
        log(f"[{_ts()}] Verbunden — starte Verifikation von {device}")
        log("=" * 50)

        log("\n▶ Partitionstabelle")
        out = _exec_out(ssh, password, f"parted {device} print 2>&1")
        log(out)

        log(f"\n▶ Dateisystem-Check Boot-Partition ({_part(device, 1)})")
        out = _exec_out(ssh, password, f"fsck -n {_part(device, 1)} 2>&1 | tail -8")
        log(out)

        log(f"\n▶ Dateisystem-Check Root-Partition ({_part(device, 2)})")
        out = _exec_out(ssh, password, f"fsck -n {_part(device, 2)} 2>&1 | tail -8")
        log(out)

        log("\n▶ Kritische Boot-Dateien")
        script = (
            f"mkdir -p /tmp/_vb && mount {_part(device, 1)} /tmp/_vb 2>/dev/null; "
            "for f in cmdline.txt config.txt kernel8.img; do "
            "  [ -f /tmp/_vb/$f ] && echo \"  ✓ $f\" || echo \"  ✗ $f FEHLT\"; "
            "done; umount /tmp/_vb 2>/dev/null"
        )
        out = _exec_out(ssh, password, script)
        log(out)

        log("\n▶ fstab UUID Abgleich")
        script2 = (
            f"mkdir -p /tmp/_vr && mount {_part(device, 2)} /tmp/_vr 2>/dev/null; "
            "cat /tmp/_vr/etc/fstab 2>/dev/null | grep -v '^#' | grep -v '^$'; "
            "echo '--- Partitions-UUIDs ---'; "
            f"blkid {_part(device, 1)} {_part(device, 2)} 2>/dev/null; "
            "umount /tmp/_vr 2>/dev/null"
        )
        out = _exec_out(ssh, password, script2)
        log(out)

        ssh.close()
        log("\n" + "=" * 50)
        log(f"[{_ts()}] Verifikation abgeschlossen")
        log("Wenn alle Checks ✓ und UUIDs übereinstimmen → Image bootfähig")

    except Exception as e:
        log(f"[FEHLER] {e}")
    finally:
        log_queue.put(None)
        active_verifies.pop(id(log_queue), None)


def open_image_stream(pi_id: int):
    """Opens SSH + dd|gzip stream. Returns (ssh, channel, filename) or raises."""
    conn = get_db()
    pi = conn.execute("SELECT * FROM pis WHERE id=?", (pi_id,)).fetchone()
    conn.close()
    if not pi:
        raise ValueError("Pi nicht gefunden")

    pi = dict(pi)
    password = decrypt_password(pi['password'])
    device = pi.get('backup_device') or '/dev/sda'

    ssh = _connect(pi, password)
    transport = ssh.get_transport()
    chan = transport.open_session()
    cmd = f'echo {_sq(password)} | sudo -S dd if={device} bs=4M 2>/dev/null | gzip -c'
    chan.exec_command(cmd)

    name_safe = (pi['name']
                 .replace(' ', '_').replace('(', '').replace(')', '')
                 .replace(',', '').replace('/', '-').strip('_'))
    filename = f"{name_safe}_{datetime.now().strftime('%Y-%m-%d')}.img.gz"

    return ssh, chan, filename


def get_sizes(pi_id: int):
    conn = get_db()
    pi = conn.execute("SELECT * FROM pis WHERE id=?", (pi_id,)).fetchone()
    conn.close()
    if not pi:
        return None

    try:
        password = decrypt_password(pi['password'])
        ssh = _connect(dict(pi), password)
        device = pi['backup_device'] or '/dev/sda'

        lsblk = _exec_out(ssh, password, "lsblk -o NAME,SIZE,FSUSE%,MOUNTPOINT 2>&1")
        df_main = _exec_out(ssh, password, "df -h / 2>&1")
        df_backup = _exec_out(ssh, password, f"df -h {device} 2>/dev/null || echo 'Backup-SD nicht gemountet'")
        ssh.close()
        return {'lsblk': lsblk, 'df_main': df_main, 'df_backup': df_backup}
    except Exception as e:
        return {'error': str(e)}


def _connect(pi, password):
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(pi['ip'], port=pi.get('port', 22), username=pi['user'], password=password, timeout=30)
    ssh.get_transport().set_keepalive(30)
    return ssh


def _exec(ssh, password, cmd, log_fn):
    full = f'echo {_sq(password)} | sudo -S bash -c {_sq(cmd)} 2>&1'
    _, stdout, _ = ssh.exec_command(full)
    for line in stdout:
        log_fn(line.rstrip())


def _exec_out(ssh, password, cmd) -> str:
    full = f'echo {_sq(password)} | sudo -S bash -c {_sq(cmd)} 2>&1'
    _, stdout, _ = ssh.exec_command(full)
    return stdout.read().decode('utf-8', errors='replace').strip()


def _ts():
    return datetime.now().strftime('%H:%M:%S')


def _part(device: str, n: int) -> str:
    """Returns correct partition name: sda->sda1, mmcblk0->mmcblk0p1"""
    if device.rstrip('/').endswith(tuple('0123456789')):
        return f"{device}p{n}"
    return f"{device}{n}"


def _sq(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"
