from junhyunbank.instance_guard import InstanceGuard


def test_only_one_instance_guard_can_hold_same_profile_lock(tmp_path):
    path = tmp_path / "junhyunbank.lock"
    first = InstanceGuard(path)
    second = InstanceGuard(path)

    assert first.acquire() is True
    try:
        assert second.acquire() is False
    finally:
        first.release()

    assert second.acquire() is True
    second.release()
