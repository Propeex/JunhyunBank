from junhyunbank.updater import UpdateInfo, is_update_available, parse_version


def test_version_parser_supports_release_tags():
    assert parse_version("V2") == (2,)
    assert parse_version("v2.1.3") == (2, 1, 3)


def test_newer_major_release_is_update():
    info = UpdateInfo("V99", (99,), "https://example.invalid/app.exe", "sha256:abc", "JunhyunBank.exe")
    assert is_update_available(info)


def test_older_release_is_not_update():
    info = UpdateInfo("V1", (1,), "https://example.invalid/app.exe", "sha256:abc", "JunhyunBank.exe")
    assert not is_update_available(info)
