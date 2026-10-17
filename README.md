# 🫐 rpi-image-cloner

A web-based tool to clone and backup Raspberry Pi SD cards via SSH — with automated weekly schedules, boot-partition auto-fix, and a clean browser UI.

**DE:** Web-Tool zum Klonen und Backup von Raspberry Pi SD-Karten via SSH — mit automatischen Wochenplänen, Boot-Partition Auto-Fix und einer übersichtlichen Browser-Oberfläche.

---

## ✨ Features

- **Automated backups** — weekly schedules via cron, runs while you sleep
- **Boot auto-fix** — automatically repairs `cmdline.txt` and `fstab` after clone (correct PARTUUID)
- **Progress monitoring** — live progress display with elapsed time and data copied
- **Backup verification** — checks filesystem integrity after backup
- **SSH keepalive** — stable connections even for backups taking 2+ hours
- **Encrypted credentials** — SSH passwords stored with Fernet encryption
- **Multiple servers** — manage several Raspberry Pis from one interface

---

## 🧰 Stack

| Component | Technology |
|-----------|-----------|
| Backend | Python · FastAPI · Uvicorn |
| SSH | Paramiko |
| Scheduler | APScheduler |
| Clone engine | [rpi-clone](https://github.com/billw2/rpi-clone) (must be installed on target Pi) |
| Database | SQLite |
| Frontend | HTML · Jinja2 |
| Container | Docker |

---

## 🚀 Quick Start (Docker / Portainer)

### Option 1 — docker-compose

```yaml
version: '3.8'

services:
  rpi-image-cloner:
    image: rpi-image-cloner:latest
    container_name: rpi-image-cloner
    ports:
      - "8099:8000"
    volumes:
      - rpi_cloner_data:/app/data
    environment:
      - TZ=Europe/Zurich      # ← Change to your timezone
    restart: unless-stopped

volumes:
  rpi_cloner_data:
```

### Option 2 — Build from source

```bash
git clone https://github.com/pierrecinema/rpi-image-cloner.git
cd rpi-image-cloner
docker build -t rpi-image-cloner:latest .
docker compose up -d
```

Open **http://localhost:8099** in your browser.

---

## 📋 Requirements

### On the host running rpi-image-cloner
- Docker

### On each Raspberry Pi to back up
- SSH enabled
- A backup SD card inserted (USB adapter or second card slot)
- [rpi-clone](https://github.com/billw2/rpi-clone) installed:

```bash
sudo wget https://raw.githubusercontent.com/billw2/rpi-clone/master/rpi-clone -O /usr/local/sbin/rpi-clone
sudo chmod +x /usr/local/sbin/rpi-clone
```

---

## ⚙️ Configuration

1. Open the web UI at `http://<your-host>:8099`
2. Add a Raspberry Pi with IP, SSH user and password
3. Insert a backup SD card into the Pi (USB adapter)
4. Click **Backup now** or set a weekly schedule

---

## 🔐 Security

- SSH passwords are encrypted with [Fernet](https://cryptography.io/en/latest/fernet/) before storage
- The database (`cloner.db`) is stored in a Docker volume and never exposed
- No credentials are hardcoded in the source code

---

## 📁 Project Structure

```
rpi-image-cloner/
├── main.py          # FastAPI app, routes
├── ssh_backup.py    # SSH backup logic, POST-FIX boot repair
├── scheduler.py     # APScheduler weekly jobs
├── database.py      # SQLite helpers, credential encryption
├── requirements.txt
├── templates/
│   └── index.html   # Web UI
├── Dockerfile
└── docker-compose.yml
```

---

## 🇩🇪 Deutsch

**rpi-image-cloner** ist ein selbst gehostetes Web-Tool zum automatischen Backup von Raspberry Pi SD-Karten über SSH.

**Funktionsweise:**
1. Tool verbindet sich per SSH mit dem Raspberry Pi
2. Startet `rpi-clone` auf dem Pi (klont die laufende SD-Karte auf eine Backup-Karte)
3. Repariert automatisch `cmdline.txt` und `fstab` auf der Backup-Karte (korrekte PARTUUID)
4. Die Backup-Karte ist direkt bootfähig — kein manueller Eingriff nötig

**Für Portainer-Nutzer:** Das `docker-compose.yml` kann direkt als Stack in Portainer eingefügt werden. Volume `rpi_cloner_data` speichert die Datenbank persistent.

---

## 📄 License

MIT
