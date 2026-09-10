from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from . import __version__

RELEASE_API = "https://api.github.com/repos/Propeex/JunhyunBank/releases/latest"
_HEALTH_TOKEN = re.compile(r"^[A-Za-z0-9-]{16,128}$")


@dataclass(slots=True)
class UpdateInfo:
    tag: str
    version: tuple[int, ...]
    asset_url: str
    digest: str | None
    asset_name: str


def parse_version(value: str) -> tuple[int, ...]:
    numbers = re.findall(r"\d+", value.strip().lstrip("vV"))
    if not numbers:
        raise ValueError(f"버전을 해석할 수 없습니다: {value}")
    return tuple(int(x) for x in numbers[:3])


def current_version() -> tuple[int, ...]:
    return parse_version(__version__)


def check_latest(timeout: float = 15.0) -> UpdateInfo:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": f"JunhyunBank/{__version__}",
    }
    with httpx.Client(timeout=timeout, follow_redirects=True, headers=headers) as client:
        response = client.get(RELEASE_API)
        response.raise_for_status()
        release = response.json()
    tag = str(release.get("tag_name") or "")
    asset: dict[str, Any] | None = None
    for row in release.get("assets") or []:
        if str(row.get("name") or "").lower() == "junhyunbank.exe":
            asset = row
            break
    if not tag or asset is None:
        raise RuntimeError("최신 GitHub Release에서 JunhyunBank.exe를 찾지 못했습니다.")
    return UpdateInfo(
        tag,
        parse_version(tag),
        str(asset.get("browser_download_url") or ""),
        str(asset.get("digest")) if asset.get("digest") else None,
        str(asset.get("name") or "JunhyunBank.exe"),
    )


def is_update_available(info: UpdateInfo) -> bool:
    return info.version > current_version()


def _verify_digest(path: Path, digest: str | None) -> None:
    if not digest:
        raise RuntimeError(
            "GitHub Release가 SHA-256 digest를 제공하지 않아 안전하게 업데이트할 수 없습니다."
        )
    algorithm, _, expected = digest.partition(":")
    if algorithm.lower() != "sha256" or not expected:
        raise RuntimeError(f"지원하지 않는 Release digest 형식입니다: {digest}")
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual.lower() != expected.lower():
        raise RuntimeError("다운로드한 실행파일의 SHA-256 검증에 실패했습니다.")


def _ps(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _base_dir(base_dir: Path | None = None) -> Path:
    return Path(base_dir) if base_dir is not None else Path.home() / ".junhyunbank"


def _validated_health_token(token: str) -> str:
    token = str(token or "").strip()
    if not _HEALTH_TOKEN.fullmatch(token):
        raise ValueError("업데이트 검증 토큰 형식이 올바르지 않습니다.")
    return token


def post_update_health_path(token: str, *, base_dir: Path | None = None) -> Path:
    token = _validated_health_token(token)
    return _base_dir(base_dir) / "update" / f"health-{token}.json"


def verify_post_update_install(token: str, *, base_dir: Path | None = None) -> Path:
    """Verify the newly installed binary before normal launch or auto-resume.

    This path intentionally does not access Upbit or submit orders. It opens the
    application database through the current Storage implementation (thereby
    exercising schema migrations), performs SQLite quick_check and confirms the
    critical tables exist. Only then is a token-bound health marker written.
    The PowerShell updater restores both the prior EXE and database snapshot if
    this verification process fails.
    """
    from .storage import Storage

    token = _validated_health_token(token)
    base = _base_dir(base_dir)
    base.mkdir(parents=True, exist_ok=True)
    storage = Storage(base / "junhyunbank.db")
    required_tables = {
        "events",
        "trades",
        "managed_positions",
        "strategy_outcomes",
        "order_intents",
    }
    with sqlite3.connect(storage.path, timeout=10) as conn:
        quick = conn.execute("PRAGMA quick_check").fetchone()
        if not quick or str(quick[0]).lower() != "ok":
            raise RuntimeError(f"업데이트 후 SQLite 무결성 검사 실패: {quick}")
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    tables = {str(row[0]) for row in rows}
    missing = sorted(required_tables - tables)
    if missing:
        raise RuntimeError(f"업데이트 후 필수 DB 테이블 누락: {', '.join(missing)}")

    health = post_update_health_path(token, base_dir=base)
    health.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "token": token,
        "version": __version__,
        "pid": os.getpid(),
        "verified_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "database": str(storage.path),
    }
    temporary = health.with_name(health.name + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, health)
    return health


def _build_apply_script(
    *,
    current_exe: Path,
    staged: Path,
    base: Path,
    token: str,
    expected_version: str,
    resume_trading: bool,
) -> str:
    """Build the detached PowerShell transaction used after the app exits."""
    token = _validated_health_token(token)
    update_dir = base / "update"
    health = post_update_health_path(token, base_dir=base)
    backup_exe = current_exe.with_name(current_exe.name + f".old-{token}")
    db = base / "junhyunbank.db"
    db_wal = Path(str(db) + "-wal")
    db_shm = Path(str(db) + "-shm")
    db_backup = update_dir / f"junhyunbank-{token}.db.bak"
    wal_backup = update_dir / f"junhyunbank-{token}.db-wal.bak"
    shm_backup = update_dir / f"junhyunbank-{token}.db-shm.bak"

    final_launch = (
        f"Start-Process -FilePath $current -ArgumentList @({_ps('--resume-trading')})"
        if resume_trading
        else "Start-Process -FilePath $current"
    )
    verify_args = f"@({_ps('--post-update-verify')}, {_ps(token)})"

    return f"""param([int]$ParentPid)
$ErrorActionPreference = 'Stop'
$current = {_ps(str(current_exe))}
$staged = {_ps(str(staged))}
$backupExe = {_ps(str(backup_exe))}
$db = {_ps(str(db))}
$dbWal = {_ps(str(db_wal))}
$dbShm = {_ps(str(db_shm))}
$dbBackup = {_ps(str(db_backup))}
$walBackup = {_ps(str(wal_backup))}
$shmBackup = {_ps(str(shm_backup))}
$health = {_ps(str(health))}
$token = {_ps(token)}
$expectedVersion = {_ps(expected_version)}

for ($i = 0; $i -lt 120; $i++) {{
  if (-not (Get-Process -Id $ParentPid -ErrorAction SilentlyContinue)) {{ break }}
  Start-Sleep -Milliseconds 250
}}
if (Get-Process -Id $ParentPid -ErrorAction SilentlyContinue) {{ exit 10 }}

$hadDb = Test-Path $db
$hadWal = Test-Path $dbWal
$hadShm = Test-Path $dbShm
if (Test-Path $health) {{ Remove-Item -Force $health }}
if (Test-Path $dbBackup) {{ Remove-Item -Force $dbBackup }}
if (Test-Path $walBackup) {{ Remove-Item -Force $walBackup }}
if (Test-Path $shmBackup) {{ Remove-Item -Force $shmBackup }}
if ($hadDb) {{ Copy-Item -Force $db $dbBackup }}
if ($hadWal) {{ Copy-Item -Force $dbWal $walBackup }}
if ($hadShm) {{ Copy-Item -Force $dbShm $shmBackup }}

function Restore-Previous {{
  try {{
    if (Test-Path $current) {{ Remove-Item -Force $current }}
    if (Test-Path $backupExe) {{ Move-Item -Force $backupExe $current }}
    if (Test-Path $db) {{ Remove-Item -Force $db }}
    if (Test-Path $dbWal) {{ Remove-Item -Force $dbWal }}
    if (Test-Path $dbShm) {{ Remove-Item -Force $dbShm }}
    if ($hadDb -and (Test-Path $dbBackup)) {{ Copy-Item -Force $dbBackup $db }}
    if ($hadWal -and (Test-Path $walBackup)) {{ Copy-Item -Force $walBackup $dbWal }}
    if ($hadShm -and (Test-Path $shmBackup)) {{ Copy-Item -Force $shmBackup $dbShm }}
  }} catch {{ }}
}}

try {{
  if (Test-Path $backupExe) {{ Remove-Item -Force $backupExe }}
  if (Test-Path $current) {{ Move-Item -Force $current $backupExe }}
  Move-Item -Force $staged $current
}} catch {{
  Restore-Previous
  exit 2
}}

try {{
  $verify = Start-Process -FilePath $current -ArgumentList {verify_args} -PassThru -Wait
  if ($verify.ExitCode -ne 0) {{ throw "new binary verification exited $($verify.ExitCode)" }}
  if (-not (Test-Path $health)) {{ throw "new binary did not create health marker" }}
  $healthData = Get-Content -Raw $health | ConvertFrom-Json
  if ($healthData.token -ne $token) {{ throw "health token mismatch" }}
  if ($healthData.version -ne $expectedVersion) {{ throw "health version mismatch" }}
}} catch {{
  Restore-Previous
  if (Test-Path $current) {{ Start-Process -FilePath $current }}
  exit 3
}}

{final_launch}
Start-Sleep -Seconds 1
if (Test-Path $backupExe) {{ Remove-Item -Force $backupExe -ErrorAction SilentlyContinue }}
if (Test-Path $dbBackup) {{ Remove-Item -Force $dbBackup -ErrorAction SilentlyContinue }}
if (Test-Path $walBackup) {{ Remove-Item -Force $walBackup -ErrorAction SilentlyContinue }}
if (Test-Path $shmBackup) {{ Remove-Item -Force $shmBackup -ErrorAction SilentlyContinue }}
if (Test-Path $health) {{ Remove-Item -Force $health -ErrorAction SilentlyContinue }}
Remove-Item -Force $PSCommandPath -ErrorAction SilentlyContinue
"""


def stage_and_launch_update(
    info: UpdateInfo, *, resume_trading: bool, timeout: float = 120.0
) -> None:
    if not getattr(sys, "frozen", False):
        raise RuntimeError("자동 업데이트는 Windows 배포 실행파일에서만 사용할 수 있습니다.")
    if not info.asset_url:
        raise RuntimeError("Release 다운로드 주소가 없습니다.")

    current_exe = Path(sys.executable).resolve()
    base = _base_dir()
    update_dir = base / "update"
    update_dir.mkdir(parents=True, exist_ok=True)
    token = str(uuid.uuid4())
    staged = update_dir / f"JunhyunBank.{token}.new.exe"
    script = update_dir / f"apply-update-{token}.ps1"
    headers = {
        "Accept": "application/octet-stream",
        "User-Agent": f"JunhyunBank/{__version__}",
    }
    with httpx.Client(timeout=timeout, follow_redirects=True, headers=headers) as client:
        with client.stream("GET", info.asset_url) as response:
            response.raise_for_status()
            with staged.open("wb") as handle:
                for chunk in response.iter_bytes():
                    if chunk:
                        handle.write(chunk)
    _verify_digest(staged, info.digest)

    expected_version = ".".join(str(part) for part in info.version)
    powershell = _build_apply_script(
        current_exe=current_exe,
        staged=staged,
        base=base,
        token=token,
        expected_version=expected_version,
        resume_trading=resume_trading,
    )
    script.write_text(powershell, encoding="utf-8-sig")
    flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            "-ParentPid",
            str(os.getpid()),
        ],
        close_fds=True,
        creationflags=flags,
    )
