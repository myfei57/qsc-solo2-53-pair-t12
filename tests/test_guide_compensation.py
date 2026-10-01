"""导焦栅对位漂移补偿：趋势补偿、超限策略、标定迁移与复查核对。"""

from __future__ import annotations

import dataclasses
import json

import pytest

from coke.app import build_platform
from coke.config import PlatformConfig, config_to_mapping, default_config, load_config
from coke.console.api import Api
from coke.errors import AlignmentError, ConfigError, StorageError


def _push_with_wear(platform, wear_mm: float, oven: str = "oven01", target_mm: float = 0.0) -> dict:
    """模拟机械磨损：车实际停位总比补偿后目标多偏 wear_mm。"""

    plan = platform.guide_comp.plan(target_mm)
    measured = plan["commanded_mm"] + wear_mm
    return platform.push(oven=oven, guide_target_mm=target_mm, guide_measured_mm=measured)


def _config_with_unit_policy(policy: dict[str, str]):
    config = default_config()
    compensation = dataclasses.replace(config.guide.compensation, unit_policies=policy)
    return dataclasses.replace(config, guide=dataclasses.replace(config.guide, compensation=compensation))


def test_compensation_tracks_constant_drift(platform):
    platform.start()
    for _ in range(3):
        report = _push_with_wear(platform, 3.0)
        assert report["ok"] is True
    status = platform.guide_comp.status()
    assert status["offset_mm"] == -3.0
    assert status["drift_mm"] == 3.0
    assert status["overrun"] is False
    decision = report["compensation"]
    assert decision["epoch"] == 0
    assert decision["window"] == [3.0, 3.0, 3.0]
    assert decision["offset_before_mm"] == -3.0
    assert decision["offset_after_mm"] == -3.0
    assert decision["clamped"] is False
    entries = platform.audit.entries(limit=50)
    compensated = [entry for entry in entries if entry["action"] == "guide_compensated"]
    assert compensated and all(entry["payload"]["epoch"] == 0 for entry in compensated)


def test_compensation_offsets_wear_so_alignment_holds(platform):
    platform.start()
    # 磨损 9mm：未超限但已吃掉大半公差；补偿量跟着漂移走（限幅 8mm），对位保持合格。
    for _ in range(3):
        report = _push_with_wear(platform, 9.0)
        assert report["ok"] is True
    decision = report["compensation"]
    assert decision["offset_after_mm"] == -8.0
    assert decision["clamped"] is True
    assert decision["overrun"] is False
    assert decision["halted"] is False


def test_overrun_halts_until_recalibration(platform):
    platform.start()
    report = _push_with_wear(platform, 12.0)
    assert report["ok"] is True
    assert report["compensation"]["overrun"] is True
    assert report["compensation"]["halted"] is True
    with pytest.raises(AlignmentError):
        platform.guide_comp.plan(0.0)
    blocked = platform.push(oven="oven01", guide_target_mm=0.0, guide_measured_mm=0.0)
    assert blocked["ok"] is False
    assert "guide_recalibration_required" in blocked["violations"]
    alarms = platform.audit.alarms()
    overrun = [entry for entry in alarms if entry["action"] == "guide_drift_overrun"]
    assert overrun and overrun[0]["payload"]["handling"] == "halt"
    assert any(entry["action"] == "interlock_rejected" for entry in alarms)
    # 重新标定后解除锁存，推焦恢复。
    platform.recalibrate_guide(0.0)
    assert platform.guide_comp.halted() is False
    resumed = _push_with_wear(platform, 2.0)
    assert resumed["ok"] is True


def test_overrun_policy_continue_is_per_unit(tmp_path, clock):
    config = _config_with_unit_policy({"battery1": "continue"})
    platform = build_platform(config, clock=clock, storage_root=str(tmp_path / "state"))
    platform.start()
    report = _push_with_wear(platform, 12.0)
    assert report["compensation"]["overrun"] is True
    assert report["compensation"]["halted"] is False
    assert report["compensation"]["clamped"] is True
    # continue 策略不停机：下一次推焦照常进行，补偿量限幅在 8mm。
    followup = _push_with_wear(platform, 12.0)
    assert followup["ok"] is True
    assert followup["compensation"]["offset_after_mm"] == -8.0
    status = platform.guide_comp.status()
    assert status["policy"] == "continue"
    assert status["halted"] is False
    alarms = [entry for entry in platform.audit.alarms() if entry["action"] == "guide_drift_overrun"]
    assert alarms and alarms[0]["payload"]["handling"] == "continue"


def test_recalibration_is_versioned_migration(platform):
    platform.start()
    _push_with_wear(platform, 3.0)
    _push_with_wear(platform, 3.0)
    migration = platform.recalibrate_guide(0.5)
    assert migration["from_epoch"] == 0
    assert migration["to_epoch"] == 1
    assert migration["baseline_after_mm"] == 0.5
    assert migration["offset_reset_mm"] == -3.0
    assert migration["samples_cleared"] == 2
    assert migration["decisions_kept"] == 2
    status = platform.guide_comp.status()
    assert status["epoch"] == 1
    assert status["offset_mm"] == 0.0
    assert status["samples"] == 0
    report = _push_with_wear(platform, 3.0)
    assert report["compensation"]["epoch"] == 1
    assert report["compensation"]["offset_before_mm"] == 0.0
    entries = platform.audit.entries(limit=50)
    migrated = [entry for entry in entries if entry["action"] == "guide_baseline_migrated"]
    assert migrated and migrated[0]["payload"]["to_epoch"] == 1


def test_compensation_state_survives_restart(tmp_path, clock):
    storage = str(tmp_path / "state")
    first = build_platform(default_config(), clock=clock, storage_root=storage)
    first.start()
    _push_with_wear(first, 3.0)
    _push_with_wear(first, 3.0)

    second = build_platform(default_config(), clock=clock, storage_root=storage)
    status = second.guide_comp.status()
    assert status["offset_mm"] == -3.0
    assert status["samples"] == 2
    assert status["decisions_logged"] == 2
    # 状态按炉组分文档落盘。
    assert "guide_compensation_battery1" in second.store.list_ids()


def test_schema_migration_on_load(tmp_path, clock):
    storage = tmp_path / "state"
    storage.mkdir()
    legacy = {
        "schema": 1,
        "unit": "battery1",
        "epoch": 2,
        "baseline_mm": 0.1,
        "compensation_mm": -1.5,
        "samples": [{"ts": 1.0, "oven": "oven01", "deviation_mm": 1.5}],
        "overrun": False,
    }
    (storage / "guide_compensation_battery1.json").write_text(
        json.dumps(legacy), encoding="utf-8"
    )
    platform = build_platform(default_config(), clock=clock, storage_root=str(storage))
    status = platform.guide_comp.status()
    assert status["epoch"] == 2
    assert status["offset_mm"] == -1.5
    persisted = platform.store.read("guide_compensation_battery1")
    assert persisted["schema"] == 2
    assert persisted["decisions"] == []
    entries = platform.audit.entries(limit=10)
    assert any(entry["action"] == "guide_compensation_migrated" for entry in entries)


def test_compensation_state_of_other_unit_is_refused(tmp_path, clock):
    storage = tmp_path / "state"
    storage.mkdir()
    foreign = {"schema": 2, "unit": "battery2", "epoch": 0, "baseline_mm": 0.0, "offset_mm": 1.0}
    (storage / "guide_compensation_battery1.json").write_text(
        json.dumps(foreign), encoding="utf-8"
    )
    with pytest.raises(StorageError):
        build_platform(default_config(), clock=clock, storage_root=str(storage))


def test_review_confirms_consistent_history(platform):
    platform.start()
    for _ in range(3):
        _push_with_wear(platform, 3.0)
    platform.recalibrate_guide(0.5)
    for _ in range(2):
        _push_with_wear(platform, 1.0)
    review = platform.review_guide_compensation()
    assert review["consistent"] is True
    assert review["decisions_checked"] == 5
    assert review["migrations_checked"] == 1
    assert review["mismatches"] == []
    assert review["audit_missing"] == []
    assert review["missing_migrations"] == []


def test_review_flags_tampered_decision(tmp_path, clock):
    storage = str(tmp_path / "state")
    first = build_platform(default_config(), clock=clock, storage_root=storage)
    first.start()
    _push_with_wear(first, 3.0)
    _push_with_wear(first, 3.0)
    tampered = first.store.read("guide_compensation_battery1")
    tampered["decisions"][0]["offset_after_mm"] = -9.9
    first.store.write("guide_compensation_battery1", tampered)

    second = build_platform(default_config(), clock=clock, storage_root=storage)
    review = second.review_guide_compensation()
    assert review["consistent"] is False
    assert review["mismatches"]
    assert review["audit_missing"]


def test_console_exposes_compensation_review_and_recalibrate(platform):
    platform.start()
    _push_with_wear(platform, 3.0)
    api = Api(platform)
    status, payload = api.handle("GET", "/api/guide/compensation", {}, {})
    assert status == 200
    assert payload["offset_mm"] == -3.0
    assert payload["decisions"] and payload["decisions"][0]["epoch"] == 0
    status, migration = api.handle("POST", "/api/guide/recalibrate", {}, {"baseline_mm": 0.5})
    assert status == 200
    assert migration["to_epoch"] == 1
    status, review = api.handle("GET", "/api/guide/compensation/review", {}, {})
    assert status == 200
    assert review["consistent"] is True
    paths = [route["path"] for route in api.routes()]
    assert "/api/guide/compensation" in paths
    assert "/api/guide/recalibrate" in paths


def test_status_exposes_compensation(platform):
    platform.start()
    _push_with_wear(platform, 3.0)
    compensation = platform.status()["guide"]["compensation"]
    assert compensation["epoch"] == 0
    assert compensation["offset_mm"] == -3.0
    assert compensation["policy"] == "halt"


def test_config_compensation_defaults_and_round_trip():
    config = default_config()
    compensation = config.guide.compensation
    assert compensation.window >= 1
    assert compensation.policy_for("battery1") == "halt"
    assert compensation.policy_for("any-other") == "halt"
    payload = config_to_mapping(config)
    loaded = PlatformConfig.from_mapping(payload)
    assert loaded.guide == config.guide


def test_config_compensation_validation():
    payload = config_to_mapping(default_config())
    payload["limits"]["guide"]["compensation"]["overrun_policy"] = "maybe"
    with pytest.raises(ConfigError):
        PlatformConfig.from_mapping(payload)

    payload = config_to_mapping(default_config())
    payload["limits"]["guide"]["compensation"]["unit_policies"] = {"battery1": "maybe"}
    with pytest.raises(ConfigError):
        PlatformConfig.from_mapping(payload)

    payload = config_to_mapping(default_config())
    payload["limits"]["guide"]["compensation"]["window"] = 2.5
    with pytest.raises(ConfigError):
        PlatformConfig.from_mapping(payload)

    payload = config_to_mapping(default_config())
    del payload["limits"]["guide"]["compensation"]
    with pytest.raises(ConfigError):
        PlatformConfig.from_mapping(payload)


def test_shipped_config_sets_policy_per_unit():
    config = load_config("config/coke.json")
    compensation = config.guide.compensation
    assert compensation.policy_for("battery1") == "continue"
    assert compensation.policy_for("battery2") == "halt"
