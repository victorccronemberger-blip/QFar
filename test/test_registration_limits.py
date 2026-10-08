"""Offline provider cooldowns survive interruptions and reject corrupt state."""
import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest

from moneymin import config
from moneymin.atomic_io import JsonStateError
from moneymin.web import registration_limits, registration_state


@pytest.fixture
def cooldown(tmp_path, monkeypatch):
    assert "venom-offline-" in os.environ.get("QMONEY_USER_ROOT", "")
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(config, "DATA_DIR", data)
    monkeypatch.setattr(registration_limits, "_observed", {})
    clock = [1000.25]
    monkeypatch.setattr(registration_limits.time, "time", lambda: clock[0])
    return data / "account_registration_limits.json", clock


def test_default_is_provider_wide_durable_and_expires_without_rewriting(cooldown):
    path, clock = cooldown
    assert registration_limits.check_all() is None
    assert not path.exists()
    assert registration_limits.defer("crowtado", None) == 300
    before = path.read_bytes()
    assert registration_limits.remaining("minute") == 0
    assert registration_limits.check_all() == ("crowtado", 300)
    clock[0] += 299.1
    assert registration_limits.remaining("crowtado") == 1
    clock[0] += 0.9
    assert registration_limits.check_all() is None
    assert path.read_bytes() == before
    # The file alone reconstructs the cooldown even after an in-memory reset.
    with patch.object(registration_limits, "_LOCK", threading.RLock()):
        clock[0] = 1010.25
        assert registration_limits.remaining("crowtado") == 290


def test_fractional_retry_is_ceiled_and_shorter_response_cannot_remove_cooldown(cooldown):
    _, clock = cooldown
    assert registration_limits.defer("minute", 2.1) == 3
    clock[0] += 0.11
    assert registration_limits.defer("minute", 0) == 2
    assert registration_limits.defer("minute", 5.2) == 6
    assert registration_limits.check_all() == ("minute", 6)


def test_explicit_zero_is_respected_and_both_providers_keep_their_deadlines(cooldown):
    _, _clock = cooldown
    assert registration_limits.defer("crowtado", 0) == 0
    assert registration_limits.check_all() is None
    registration_limits.defer("minute", 5)
    registration_limits.defer("crowtado", 3)
    assert registration_limits.check_all() == ("crowtado", 3)
    assert registration_limits.remaining("minute") == 5


@pytest.mark.parametrize("value", [True, False, -1, float("nan"), float("inf"),
                                  -float("inf"), "300", [], {}, 10 ** 1000])
def test_invalid_retry_does_not_replace_prior_cooldown(cooldown, value):
    path, _clock = cooldown
    registration_limits.defer("minute", 60)
    before = path.read_bytes()
    with pytest.raises(ValueError):
        registration_limits.defer("crowtado", value)
    assert path.read_bytes() == before
    assert registration_limits.remaining("minute") == 60


@pytest.mark.parametrize("provider", ["unknown", "Minute", "", None, [], True])
def test_invalid_provider_never_reads_or_writes_store(cooldown, provider):
    path, _clock = cooldown
    with patch.object(registration_limits, "load_json_state") as read:
        for operation in (lambda: registration_limits.defer(provider, 10),
                          lambda: registration_limits.remaining(provider)):
            with pytest.raises(ValueError):
                operation()
        read.assert_not_called()
    assert not path.exists()


@pytest.mark.parametrize("raw", [
    b'{invalid',
    b'{"schema":true,"providers":{}}',
    b'{"schema":1,"schema":1,"providers":{}}',
    b'{"schema":1,"providers":{"minute":{"until_epoch_s":1,"until_epoch_s":2}}}',
    b'{"schema":1,"providers":{"minute":{"until_epoch_s":true}}}',
    b'{"schema":1,"providers":{"minute":{"until_epoch_s":NaN}}}',
    b'{"schema":1,"providers":{"minute":{"until_epoch_s":1e999}}}',
    b'{"schema":1,"providers":{"minute":{"until_epoch_s":-1}}}',
    b'{"schema":1,"providers":{"unknown":{"until_epoch_s":3}}}',
    b'{"schema":1,"providers":{"minute":{"until_epoch_s":3,"password":"fixture"}}}',
    b'{"schema":1,"providers":[],"password":"fixture"}',
])
def test_corrupt_store_is_preserved_for_every_api(cooldown, raw):
    path, _clock = cooldown
    path.write_bytes(raw)
    for operation in (lambda: registration_limits.defer("minute", 60),
                      lambda: registration_limits.remaining("minute"),
                      registration_limits.check_all):
        with pytest.raises(JsonStateError):
            operation()
        assert path.read_bytes() == raw


def test_atomic_write_failure_preserves_previous_cooldown(cooldown):
    path, _clock = cooldown
    registration_limits.defer("minute", 60)
    before = path.read_bytes()
    with patch.object(registration_limits, "save_json", side_effect=OSError("fixture failure")):
        with pytest.raises(OSError):
            registration_limits.defer("crowtado", 300)
    assert path.read_bytes() == before
    assert registration_limits.remaining("crowtado") == 300
    assert registration_limits.remaining("minute") == 60
    assert registration_limits.check_all() == ("crowtado", 300)


def test_failed_first_publication_still_blocks_until_observed_deadline(cooldown):
    path, clock = cooldown
    with patch.object(registration_limits, "save_json", side_effect=OSError("fixture failure")):
        with pytest.raises(OSError):
            registration_limits.defer("crowtado")
    assert not path.exists()
    assert registration_limits.remaining("crowtado") == 300
    assert registration_limits.check_all() == ("crowtado", 300)
    clock[0] += 300
    assert registration_limits.check_all() is None


@pytest.mark.parametrize("step,provider", [
    ("minute_identity", "minute"), ("minute_register", "minute"), ("validate", "minute"),
    ("proxy", "crowtado"), ("ban_check", "crowtado"), ("save_partial", "crowtado"),
    ("crowtado_signup", "crowtado"), ("demographics", "crowtado"), ("link_minute", "crowtado"),
])
def test_restart_recovers_provider_limit_from_validated_checkpoint(cooldown, step, provider):
    path, _clock = cooldown
    with patch.object(registration_limits, "save_json", side_effect=OSError("fixture failure")):
        with pytest.raises(OSError):
            registration_limits.defer(provider, 100)
    registration_state.update("fixture@example.invalid", state="incomplete", steps={
        step: {"status": "fail", "detail": "Provider limited registration.",
               "code": "rate_limit", "retry_at": 1300, "retry_after_seconds": 300},
    })
    checkpoint_path = path.parent / "account_registrations.json"
    before = checkpoint_path.read_bytes()
    registration_limits._observed.clear()
    assert registration_limits.remaining(provider) == 300
    assert registration_limits.check_all() == (provider, 300)
    assert checkpoint_path.read_bytes() == before
    assert not path.exists()


def test_reads_and_new_response_use_longest_persisted_ram_or_checkpoint_deadline(cooldown):
    path, _clock = cooldown
    registration_limits.defer("minute", 100)
    before = path.read_bytes()
    with patch.object(registration_limits, "save_json", side_effect=OSError("fixture failure")):
        with pytest.raises(OSError):
            registration_limits.defer("minute", 200)
    registration_state.update("fixture@example.invalid", state="incomplete", steps={
        "minute_identity": {"status": "fail", "code": "rate_limit", "retry_at": 1400},
    })
    assert registration_limits.remaining("minute") == 400
    assert path.read_bytes() == before
    assert registration_limits.defer("minute", 10) == 400
    assert registration_limits.remaining("minute") == 400


def test_observed_limit_cannot_hide_corrupt_limits_or_registration_checkpoint(cooldown):
    path, _clock = cooldown
    registration_limits.defer("minute", 60)
    valid = path.read_bytes()
    path.write_bytes(b'{"schema":true,"providers":{}}')
    for operation in (lambda: registration_limits.remaining("minute"), registration_limits.check_all):
        with pytest.raises(JsonStateError):
            operation()
    path.write_bytes(valid)
    checkpoint_path = path.parent / "account_registrations.json"
    invalid = b'{"fixture@example.invalid":{"email":"fixture@example.invalid","state":"incomplete","identity":{},"steps":{"minute_identity":{"status":"fail","code":"rate_limit","retry_at":true}}}}'
    checkpoint_path.write_bytes(invalid)
    for operation in (lambda: registration_limits.defer("minute", 80),
                      lambda: registration_limits.remaining("minute"), registration_limits.check_all):
        with pytest.raises(JsonStateError):
            operation()
        assert path.read_bytes() == valid
        assert checkpoint_path.read_bytes() == invalid


def test_ram_observation_is_scoped_to_store_root(cooldown, tmp_path, monkeypatch):
    path, _clock = cooldown
    with patch.object(registration_limits, "save_json", side_effect=OSError("fixture failure")):
        with pytest.raises(OSError):
            registration_limits.defer("minute", 60)
    another_root = tmp_path / "another-data-root"
    another_root.mkdir()
    with monkeypatch.context() as selected:
        selected.setattr(config, "DATA_DIR", another_root)
        assert registration_limits.check_all() is None
        assert registration_limits.remaining("minute") == 0
    assert registration_limits.check_all() == ("minute", 60)
    assert not path.exists()


@pytest.mark.parametrize("clock_value", [True, -1, float("nan"), float("inf")])
def test_invalid_wallclock_cannot_publish_or_bypass_deadline(cooldown, clock_value):
    path, clock = cooldown
    registration_limits.defer("minute", 60)
    before = path.read_bytes()
    clock[0] = clock_value
    for operation in (lambda: registration_limits.defer("minute", 20),
                      lambda: registration_limits.remaining("minute"),
                      registration_limits.check_all):
        with pytest.raises(ValueError):
            operation()
        assert path.read_bytes() == before


def test_concurrent_provider_responses_preserve_both_longest_deadlines(cooldown):
    path, _clock = cooldown
    barrier = threading.Barrier(4)

    def defer(provider, duration):
        barrier.wait(timeout=3)
        return registration_limits.defer(provider, duration)

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(defer, provider, duration) for provider, duration in (
            ("minute", 30), ("crowtado", 20), ("minute", 50), ("crowtado", 10))]
        assert all(future.result(timeout=3) > 0 for future in futures)
    assert registration_limits.remaining("minute") == 50
    assert registration_limits.remaining("crowtado") == 20
    assert json.loads(path.read_bytes())["schema"] == 1
