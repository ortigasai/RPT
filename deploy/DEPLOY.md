# RPT Assessment — server deployment

Two tiers on the server:

| Tier | What | Port | Managed by |
|------|------|------|-----------|
| Frontend | IIS site **`RPT`**, reverse proxy | **7272** (`http://*:7272`) | IIS + URL Rewrite + ARR |
| Backend | Flask app (`app.py`) | **7373** (`127.0.0.1` only) | NSSM service **`RPT-Backend`** |

`5173` is left alone (already used on the server). Only **7272** is opened on the firewall; 7373 is internal.

App root on the server: **`C:\RPT`**
Repo: `https://github.com/ortigasai/RPT.git`

```
C:\RPT\
  app.py  rpt\  templates\  requirements.txt   <- the app (from git or migration)
  broker_config.json         <- the API key (NOT in git; comes via migration)
  .venv\                     <- created by Deploy-Server.ps1
  home\.paddleocr\           <- PaddleOCR model weights (HOME for the service)
  vendor\Tesseract-OCR\      <- bundled fallback OCR engine (optional)
  iis\web.config             <- generated: the reverse-proxy rule
  tools\nssm.exe             <- fetched by Deploy-Server.ps1
  logs\                      <- backend.out.log / backend.err.log
  deploy\                    <- these scripts
```

---

## First-time deploy

### 1. Get the code + data onto the server

**Option A — migration bundle (recommended, fully offline):**

On the **dev laptop**:
```powershell
cd "C:\Users\villegaskmp\Desktop\RPT assessment"
powershell -ExecutionPolicy Bypass -File .\deploy\Export-Migration.ps1
# -> RPT-migration-<date>.zip on the Desktop
```
Copy that zip to the server, then in an **elevated** PowerShell on the server:
```powershell
powershell -ExecutionPolicy Bypass -File <extracted>\deploy\Import-Migration.ps1 -Bundle C:\Temp\RPT-migration-<date>.zip
```
`Import-Migration.ps1` lays everything into `C:\RPT` and then runs `Deploy-Server.ps1` automatically.

**Option B — git clone (needs internet on the server + the API key added by hand):**
```powershell
git clone https://github.com/ortigasai/RPT.git C:\RPT
copy C:\RPT\broker_config.example.json C:\RPT\broker_config.json
notepad C:\RPT\broker_config.json        # paste the real PERM-REG-API key
powershell -ExecutionPolicy Bypass -File C:\RPT\deploy\Deploy-Server.ps1
```

### 2. What `Deploy-Server.ps1` does (idempotent — safe to re-run)

1. builds `C:\RPT\.venv` from `requirements.txt`
2. finds Tesseract (vendor copy / system / winget)
3. warms the PaddleOCR models into `C:\RPT\home\.paddleocr`
4. registers NSSM service **`RPT-Backend`** → `python app.py` with
   `RPT_PORT=7373 RPT_HOST=127.0.0.1` and `USERPROFILE=C:\RPT\home`
5. installs IIS + URL Rewrite + ARR, enables the ARR proxy, creates app pool
   **`RPT`** (No Managed Code) and site **`RPT`** on `http://*:7272` with the
   reverse-proxy `web.config`
6. opens the firewall for TCP 7272 and health-checks both tiers

When it finishes it prints `http://<server-ip>:7272`.

---

## Updating an already-deployed server

From the **dev laptop** (keeps the server byte-for-byte identical to dev):
```powershell
.\deploy\Sync-DevToServer.ps1 -ServerHost <SERVER> -RestartService
# add -IncludeModels if the OCR model weights changed
```
or on the server: pull and restart —
```powershell
cd C:\RPT ; git pull ; Restart-Service RPT-Backend
```

Re-running `Deploy-Server.ps1` also picks up code changes (it just rebuilds the
venv and restarts the service).

---

## Operate

```powershell
Get-Service RPT-Backend
Restart-Service RPT-Backend
Get-Content C:\RPT\logs\backend.err.log -Tail 50
C:\RPT\tools\nssm.exe edit RPT-Backend      # GUI for env vars / paths

Import-Module WebAdministration
Get-Website RPT
Restart-WebAppPool RPT
```

Health:
```powershell
Invoke-WebRequest http://127.0.0.1:7373/ -UseBasicParsing   # backend direct
Invoke-WebRequest http://127.0.0.1:7272/ -UseBasicParsing   # through IIS
```

Remove everything (leaves the files):
```powershell
powershell -ExecutionPolicy Bypass -File C:\RPT\deploy\Uninstall-Server.ps1
```

---

## Notes / gotchas

- **API key**: `broker_config.json` is deliberately **not** in git. It travels
  only in the migration bundle, or you paste it on the server. Rotate it in
  the Prompters broker if it ever leaks.
- **PaddleOCR + services**: the service runs as *LocalSystem*, whose `~` is not
  writable, so `Deploy-Server.ps1` sets `USERPROFILE=C:\RPT\home`. If you'd
  rather run the service as a named account, set it with
  `nssm set RPT-Backend ObjectName <DOMAIN\user> <password>` and give that
  account read/write on `C:\RPT`.
- **First request after a restart** can take ~60 s while PaddleOCR loads.
- **Large PDFs**: IIS `web.config` allows 500 MB uploads; the Flask app caps at
  200 MB — raise `MAX_CONTENT_LENGTH` in `app.py` if needed.
- **ARR download URLs** in `Deploy-Server.ps1` are the Microsoft permalinks for
  URL Rewrite 2.1 and ARR 3.0; if Microsoft moves them, install both from the
  IIS "Web Platform" / iis.net and re-run with `-SkipIIS` off.
