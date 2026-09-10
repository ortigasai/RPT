# RPT Assessment — server deployment

Same shape as **cwt-tax-portal**, adjusted for this app (one Python/Flask
process instead of a Node client+server split).

| Tier | What | Port | Managed by |
|------|------|------|-----------|
| Front | IIS site **`RPT`** — reverse proxy | **7272** (`http://*:7272`) | IIS + URL Rewrite + ARR |
| Back | Flask app (`app.py`) | **7373** (`127.0.0.1` only) | NSSM service **`RptAssessmentServer`** |
| Data | PostgreSQL database **`RPT`** | `192.168.0.215:5432` | the tax server's Postgres (shared with cwt-tax-portal) |

`5173` is left alone (used on the server already). Only **7272** is opened on the firewall.

- App root on the server: **`C:\RPT`**
- Repo: `https://github.com/ortigasai/RPT.git` (branch `main`)
- Config: **`C:\RPT\.env`** (git-ignored — carries `DATABASE_URL`, the broker API key, the optional shared login). Template: `.env.production.example`.

```
C:\RPT\
  app.py  rpt\  templates\  requirements.txt   deploy\   <- from git
  .env                       <- config + secrets (NOT git; via migrate-data.ps1)
  broker_config.json         <- legacy fallback for the API key (optional)
  .venv\                     <- deploy-iis.ps1 builds this
  home\.paddleocr\           <- PaddleOCR model weights (HOME for the service)
  vendor\Tesseract-OCR\      <- bundled fallback OCR engine (optional)
  iis\web.config             <- generated: the reverse-proxy rule
  tools\nssm.exe             <- fetched by deploy-iis.ps1
  logs\                      <- service-out.log / service-err.log
  migration\                 <- pg_dump dropped by migrate-data.ps1
```

The three deploy scripts mirror cwt-tax-portal:

| Script | Where | Purpose |
|--------|-------|---------|
| `deploy/pull-latest.ps1` | server | `git clone` / `git pull` into `C:\RPT` |
| `deploy/migrate-data.ps1` | dev laptop | copy code + `.env` + OCR models + Tesseract to the server, `pg_dump` the `RPT` DB |
| `deploy/deploy-iis.ps1` | server (elevated) | venv → DB schema → NSSM service → IIS + ARR site |

---

## First-time deploy

### 1. Get the code + data onto the server

From the **dev laptop** (repo root):
```powershell
powershell -ExecutionPolicy Bypass -File deploy\migrate-data.ps1 -ServerHost <SERVER>
```
This mirrors the project to `\\<SERVER>\c$\RPT`, copies `.env` (patching
`RPT_PORT`→7373, `RPT_HOST`→127.0.0.1), copies the PaddleOCR models and
Tesseract, and `pg_dump`s the `RPT` database into `C:\RPT\migration\`.

> Dev and server point at the **same** Postgres (`192.168.0.215/RPT`), so the
> dump is just a backup — no `pg_restore` needed. (If you later split them,
> the script prints the exact `pg_restore` line.)

*(Alternative: on the server run `deploy\pull-latest.ps1` to `git clone`, then
`copy .env.production.example .env` and fill it in by hand.)*

### 2. Deploy

On the **server**, in an **elevated** PowerShell:
```powershell
powershell -ExecutionPolicy Bypass -File C:\RPT\deploy\deploy-iis.ps1
```
It will, idempotently:
1. build `C:\RPT\.venv` from `requirements.txt`
2. run `python -m rpt.db upgrade` — creates the `extraction_run` table in the
   `RPT` database (cwt's `prisma migrate deploy` analogue)
3. warm the PaddleOCR models into `C:\RPT\home\.paddleocr`
4. install/update the NSSM service **`RptAssessmentServer`** → `python app.py`
   with `RPT_PORT=7373 RPT_HOST=127.0.0.1 USERPROFILE=C:\RPT\home`
   (everything else — `DATABASE_URL`, broker key, `AUTH_*` — the app reads from `.env`)
5. install IIS + URL Rewrite + ARR, enable ARR proxying, create app pool
   **`RPT`** (No Managed Code) and site **`RPT`** on `http://*:7272` with the
   reverse-proxy `web.config`
6. open the firewall for TCP 7272 and health-check `/health` on both tiers

It prints `http://<server-ip>:7272` when done.

---

## Updating

**Code change** — on the server:
```powershell
cd C:\RPT
powershell -ExecutionPolicy Bypass -File deploy\pull-latest.ps1
powershell -ExecutionPolicy Bypass -File deploy\deploy-iis.ps1
```

**Code + data parity from the laptop:**
```powershell
powershell -ExecutionPolicy Bypass -File deploy\migrate-data.ps1 -ServerHost <SERVER>
# then on the server: deploy\deploy-iis.ps1   (or just: Restart-Service RptAssessmentServer)
```

---

## Operate

```powershell
Get-Service RptAssessmentServer
Restart-Service RptAssessmentServer
Get-Content C:\RPT\logs\service-err.log -Tail 50
C:\RPT\tools\nssm.exe edit RptAssessmentServer      # env vars / paths GUI

Import-Module WebAdministration
Restart-WebAppPool RPT
```

Health / DB check:
```powershell
Invoke-WebRequest http://127.0.0.1:7373/health -UseBasicParsing   # backend direct
Invoke-WebRequest http://127.0.0.1:7272/health -UseBasicParsing   # through IIS
C:\RPT\.venv\Scripts\python.exe -m rpt.db check                   # DB connectivity
```
`/health` returns `{status, ocr_backend, ocr_ready, database, auth}`.

Past runs are in Postgres: `GET /runs`, `GET /runs/<id>`, `GET /runs/<id>.xlsx`.

---

## Notes

- **Secrets** (`.env`, `broker_config.json`) are **not** in git — they reach the
  server only through `migrate-data.ps1`. `.env.production.example` is the
  committed template.
- **Shared login**: set `AUTH_USERNAME` + `AUTH_PASSWORD_HASH` in the server
  `.env` to gate the site (HTTP Basic). The example file ships the same
  `cwtportal` bcrypt hash the tax team already uses; leave both blank to run open.
- **DB optional**: if `DATABASE_URL` is unreachable the app still extracts and
  downloads — it just doesn't persist the run. `/health` shows `database: unavailable`.
- **PaddleOCR + service**: the service runs as *LocalSystem*, whose `~` isn't
  writable, so `deploy-iis.ps1` sets `USERPROFILE=C:\RPT\home`. To run as a
  named account instead: `nssm set RptAssessmentServer ObjectName <DOMAIN\user> <pw>`
  and give that account read/write on `C:\RPT`.
- **First request after a restart** can take ~60 s while PaddleOCR loads.
- **ARR/URL Rewrite MSIs**: `deploy-iis.ps1` pulls the Microsoft permalinks for
  URL Rewrite 2.1 and ARR 3.0; if those move, install both from iis.net and
  re-run.
