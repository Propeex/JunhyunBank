from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from . import __version__

RELEASE_API = "https://api.github.com/repos/Propeex/JunhyunBank/releases/latest"


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
    headers = {"Accept": "application/vnd.github+json", "User-Agent": f"JunhyunBank/{__version__}"}
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
    return UpdateInfo(tag, parse_version(tag), str(asset.get("browser_download_url") or ""), str(asset.get("digest")) if asset.get("digest") else None, str(asset.get("name") or "JunhyunBank.exe"))


def is_update_available(info: UpdateInfo) -> bool:
    return info.version > current_version()


def _verify_digest(path: Path, digest: str | None) -> None:
    if not digest:
        raise RuntimeError("GitHub Release가 SHA-256 digest를 제공하지 않아 안전하게 업데이트할 수 없습니다.")
    algorithm, _, expected = digest.partition(":")
    if algorithm.lower() != "sha256" or not expected:
        raise RuntimeError(f"지원하지 않는 Release digest 형식입니다: {digest}")
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual.lower() != expected.lower():
        raise RuntimeError("다운로드한 실행파일의 SHA-256 검증에 실패했습니다.")


def _ps(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def stage_and_launch_update(info: UpdateInfo, *, resume_trading: bool, timeout: float = 120.0) -> None:
    if not getattr(sys, "frozen", False):
        raise RuntimeError("자동 업데이트는 Windows 배포 실행파일에서만 사용할 수 있습니다.")
    if not info.asset_url:
        raise RuntimeError("Release 다운로드 주소가 없습니다.")
    current_exe = Path(sys.executable).resolve()
    base = Path.home() / ".junhyunbank" / "update"
    base.mkdir(parents=True, exist_ok=True)
    staged = base / "JunhyunBank.new.exe"
    script = base / "apply-update.ps1"
    headers = {"Accept": "application/octet-stream", "User-Agent": f"JunhyunBank/{__version__}"}
    with httpx.Client(timeout=timeout, follow_redirects=True, headers=headers) as client:
        with client.stream("GET", info.asset_url) as response:
            response.raise_for_status()
            with staged.open("wb") as handle:
                for chunk in response.iter_bytes():
                    if chunk:
                        handle.write(chunk)
    _verify_digest(staged, info.digest)
    resume_arg = "--resume-trading" if resume_trading else ""
    powershell = f"""param([int]$ParentPid)
$ErrorActionPreference = 'Stop'
$current = {_ps(str(current_exe))}
$staged = {_ps(str(staged))}
$backup = $current + '.old'
for ($i = 0; $i -lt 120; $i++) {{
  if (-not (Get-Process -Id $ParentPid -ErrorAction SilentlyContinue)) {{ break }}
  Start-Sleep -Milliseconds 250
}}
for ($i = 0; $i -lt 40; $i++) {{
  try {{
    if (Test-Path $backup) {{ Remove-Item -Force $backup }}
    if (Test-Path $current) {{ Move-Item -Force $current $backup }}
    Move-Item -Force $staged $current
    break
  }} catch {{ Start-Sleep -Milliseconds 250 }}
}}
if (-not (Test-Path $current)) {{
  if (Test-Path $backup) {{ Move-Item -Force $backup $current }}
  exit 2
}}
Start-Process -FilePath $current -ArgumentList {_ps(resume_arg)}
Start-Sleep -Seconds 2
if (Test-Path $backup) {{ Remove-Item -Force $backup -ErrorAction SilentlyContinue }}
Remove-Item -Force $PSCommandPath -ErrorAction SilentlyContinue
"""
    script.write_text(powershell, encoding="utf-8-sig")
    flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script), "-ParentPid", str(os.getpid())], close_fds=True, creationflags=flags)
