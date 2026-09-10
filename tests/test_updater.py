import json
from pathlib import Path

from junhyunbank import __version__
from junhyunbank.updater import (
    UpdateInfo,
    _build_apply_script,
    is_update_available,
    parse_version,
    verify_post_update_install,
)


def test_version_parser_supports_release_tags():
    assert parse_version("V2") == (2,)
    assert parse_version("v2.1.3") == (2, 1, 3)


def test_newer_major_release_is_update():
    info = UpdateInfo("V99", (99,), "https://example.invalid/app.exe", "sha256:abc", "JunhyunBank.exe")
    assert is_update_available(info)


def test_older_release_is_not_update():
    info = UpdateInfo("V1", (1,), "https://example.invalid/app.exe", "sha256:abc", "JunhyunBank.exe")
    assert not is_update_available(info)


def test_post_update_verification_checks_database_and_writes_token_marker(tmp_path: Path):
    token = "11111111-2222-3333-4444-555555555555"

    health = verify_post_update_install(token, base_dir=tmp_path)

    assert (tmp_path / "junhyunbank.db").exists()
    payload = json.loads(health.read_text(encoding="utf-8"))
    assert payload["token"] == token
    assert payload["version"] == __version__
    assert payload["database"] == str(tmp_path / "junhyunbank.db")


def test_apply_script_verifies_new_binary_before_resume_and_has_db_rollback(tmp_path: Path):
    token = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    current = tmp_path / "JunhyunBank.exe"
    staged = tmp_path / "update" / "JunhyunBank.new.exe"
    script = _build_apply_script(
        current_exe=current,
        staged=staged,
        base=tmp_path,
        token=token,
        expected_version="4.0.1",
        resume_trading=True,
    )

    verify_index = script.index("--post-update-verify")
    resume_index = script.index("--resume-trading")
    assert verify_index < resume_index
    assert "-PassThru -Wait" in script
    assert "$verify.ExitCode -ne 0" in script
    assert "ConvertFrom-Json" in script
    assert "health token mismatch" in script
    assert "health version mismatch" in script
    assert "function Restore-Previous" in script
    assert "Move-Item -Force $backupExe $current" in script
    assert "Copy-Item -Force $dbBackup $db" in script


def test_apply_script_does_not_resume_when_update_was_started_idle(tmp_path: Path):
    token = "ffffffff-eeee-dddd-cccc-bbbbbbbbbbbb"
    script = _build_apply_script(
        current_exe=tmp_path / "JunhyunBank.exe",
        staged=tmp_path / "new.exe",
        base=tmp_path,
        token=token,
        expected_version="4.0.1",
        resume_trading=False,
    )

    assert "--post-update-verify" in script
    assert "--resume-trading" not in script
