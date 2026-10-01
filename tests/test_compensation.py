"""导焦栅漂移补偿：按炉组独立计算、漂移趋势补偿、超限策略与版本迁移。"""

from __future__ import annotations

import pytest

from coke.compensation.conclusion import CompensationConclusion
from coke.compensation.drift import drift_stats
from coke.compensation.policy import (
    POLICY_COMPENSATE,
    POLICY_RECALIBRATE,
    SCOPE_BATTERY_GROUP,
    VIOLATION_RECALIBRATION_REQUIRED,
)
from coke.config import PlatformConfig, config_to_mapping, default_config
from coke.errors import CokeError
from coke.app import build_platform


def _drift_to_limit(comp, group, deviation_mm: float, *, oven_prefix: str = "oven", rounds: int = 25):
    """连续喂同向偏差直到窗口均值越限或停机，返回最后一条结论。"""

    conclusion = None
    for index in range(rounds):
        conclusion = comp.observe_alignment(
            group, f"{oven_prefix}{index}", 0.0, float(deviation_mm)
        )
        if comp.is_blocked(group):
            break
    return conclusion


def _push_with_overshoot(comp, group, oven, nominal_mm=0.0, overshoot_mm=8.0):
    """模拟真实执行器：收到有效目标后恒过冲 overshoot_mm。"""

    effective = comp.effective_target_mm(group, nominal_mm)
    return comp.observe_alignment(group, oven, nominal_mm, effective + overshoot_mm)


def test_drift_stats_summarises_window_trend():
    stats = drift_stats([1.0, 2.0, 3.0, 4.0])
    assert stats.samples == 4
    assert stats.mean_mm == pytest.approx(2.5)
    assert stats.slope_mm_per_sample == pytest.approx(1.0)
    assert drift_stats([]).samples == 0
    assert drift_stats([5.0]).slope_mm_per_sample == 0.0


def test_scope_is_fixed_to_battery_group(platform):
    summary = platform.guide_compensation_summary()
    assert summary["scope"] == SCOPE_BATTERY_GROUP
    assert summary["plant_wide_supported"] is False
    policies = {group: item["policy"] for group, item in summary["groups"].items()}
    assert policies == {"battery1": POLICY_RECALIBRATE, "battery2": POLICY_COMPENSATE}


def test_config_rejects_plant_wide_scope(tmp_path):
    payload = config_to_mapping(default_config())
    payload["guide_compensation"]["scope"] = "plant"
    with pytest.raises(Exception):
        PlatformConfig.from_mapping(payload)


def test_config_rejects_unknown_policy_and_missing_nominal(tmp_path):
    payload = config_to_mapping(default_config())
    payload["guide_compensation"]["group_policies"]["battery1"] = "banana"
    with pytest.raises(Exception):
        PlatformConfig.from_mapping(payload)
    payload = config_to_mapping(default_config())
    del payload["guide_compensation"]["nominal_targets_mm"]["battery1"]
    with pytest.raises(Exception):
        PlatformConfig.from_mapping(payload)


def test_compensation_learns_offset_only_after_min_samples(platform):
    comp = platform.guide_compensation
    first = comp.observe_alignment("battery1", "oven01", 0.0, 5.0)
    assert first.action == "guide_drift_monitor"
    assert first.offset_after_mm == 0.0
    comp.observe_alignment("battery1", "oven02", 0.0, 5.0)
    third = comp.observe_alignment("battery1", "oven03", 0.0, 5.0)
    assert third.offset_updated is True
    # 实测停在名义位正侧 5mm：目标位要往负侧补偿 5mm。
    assert third.offset_after_mm == pytest.approx(-5.0)
    assert comp.effective_target_mm("battery1", 0.0) == pytest.approx(-5.0)


def test_step_limit_clamps_single_correction(platform):
    comp = platform.guide_compensation
    # 机械恒过冲 +8（落在漂移限值 10 内、大于死区 2）；目标补偿 −8 单次被步长限到 −5。
    conclusions = [
        _push_with_overshoot(comp, "battery1", f"oven{index}", overshoot_mm=8.0)
        for index in range(3)
    ]
    assert conclusions[-1].offset_after_mm == pytest.approx(-5.0)
    # 下一周期再追 −3mm 到位；当炉有效目标仍是 −5，故残差 3、跟随误差仍为 8。
    settled = _push_with_overshoot(comp, "battery1", "oven03", overshoot_mm=8.0)
    assert settled.offset_after_mm == pytest.approx(-8.0)
    assert settled.residual_mm == pytest.approx(3.0)
    assert settled.follow_error_mm == pytest.approx(8.0)
    # 再一炉有效目标 −8，残差归零。
    converged = _push_with_overshoot(comp, "battery1", "oven04", overshoot_mm=8.0)
    assert converged.residual_mm == pytest.approx(0.0)


def test_recalibrate_policy_blocks_push_when_drift_over_limit(platform):
    comp = platform.guide_compensation
    conclusion = _drift_to_limit(comp, "battery1", 12.0)
    assert comp.is_blocked("battery1")
    assert conclusion.action == "guide_recalibration_required"
    assert conclusion.severity == "alarm"
    assert conclusion.offset_after_mm == conclusion.offset_before_mm

    rejected = platform.push(oven="oven99", guide_measured_mm=12.0, group="battery1")
    assert rejected["ok"] is False
    assert VIOLATION_RECALIBRATION_REQUIRED in rejected["violations"]
    assert rejected["guide_gate"]["blocked"] is True
    # 停机期间拦焦车不应被对位/锁定。
    assert platform.guide.is_locked() is False


def test_compensate_policy_keeps_running_with_warning(platform):
    comp = platform.guide_compensation
    over = None
    for index in range(25):
        over = comp.observe_alignment("battery2", f"oven{index}", 0.0, 11.0)
        if over.over_limit:
            break
    assert over is not None and over.over_limit is True
    assert over.action == "guide_target_compensated_over_limit"
    assert over.severity == "warning"
    assert comp.is_blocked("battery2") is False
    assert comp.offset_mm("battery2") < 0.0


def test_hard_cap_blocks_regardless_of_policy(platform):
    comp = platform.guide_compensation
    conclusion = _drift_to_limit(comp, "battery2", 22.0, oven_prefix="hc")
    assert comp.is_blocked("battery2")
    assert conclusion.hard_cap_exceeded is True
    assert conclusion.action == "guide_recalibration_required"


def test_compensation_drives_residual_toward_zero(platform):
    comp = platform.guide_compensation
    # 磨损让导焦栅对任何下发目标都恒过冲 +8mm；学习后偏移应收敛到 −8。
    conclusions = [
        _push_with_overshoot(comp, "battery1", f"oven{index}") for index in range(8)
    ]
    assert comp.offset_mm("battery1") == pytest.approx(-8.0)
    # 收敛后再做一次同样磨损的对位：实测停在名义位 0，残差为 0、落在公差内。
    gate_conclusion = _push_with_overshoot(comp, "battery1", "oven99")
    gate = comp.gate_alignment("battery1", "oven100", 0.0,
                               comp.effective_target_mm("battery1", 0.0) + 8.0,
                               tolerance_mm=15.0)
    assert gate_conclusion.residual_mm == pytest.approx(0.0)
    assert gate["effective_target_mm"] == pytest.approx(-8.0)
    assert gate["residual_mm"] == pytest.approx(0.0, abs=1e-6)
    assert gate["aligned"] is True and gate["blocked"] is False
    # 跟随误差始终是机械过冲 +8，正是它驱动了学习。
    assert all(abs(c.follow_error_mm - 8.0) < 1e-9 for c in conclusions)


def test_groups_are_computed_independently(platform):
    comp = platform.guide_compensation
    for index in range(3):
        comp.observe_alignment("battery1", f"oven{index}", 0.0, 5.0)
    assert comp.offset_mm("battery1") == pytest.approx(-5.0)
    # battery2 的窗口与偏移完全独立，没有被 battery1 的漂移污染。
    assert comp.offset_mm("battery2") == 0.0
    assert comp.group_summary("battery2")["stats"]["samples"] == 0


def test_recalibration_is_a_version_migration(platform):
    comp = platform.guide_compensation
    for index in range(3):
        comp.observe_alignment("battery1", f"oven{index}", 0.0, 6.0)
    migration = platform.recalibrate_guide("battery1", 12.0, 12.0, reason="检修")
    assert migration["version_from"] == 0
    assert migration["version_to"] == 1
    assert comp.baseline("battery1").version == 1
    assert comp.baseline("battery1").nominal_target_mm == 12.0
    assert comp.offset_mm("battery1") == 0.0
    assert comp.is_blocked("battery1") is False

    new = comp.observe_alignment("battery1", "oven01", 12.0, 12.4)
    assert new.calibration_version == 1
    assert new.stats.samples == 1

    migrations = comp.migrations("battery1")
    assert len(migrations) == 1
    assert migrations[0].version_to == 1


def test_blocked_group_recovers_after_migration(platform):
    comp = platform.guide_compensation
    _drift_to_limit(comp, "battery1", 12.0)
    assert comp.is_blocked("battery1")
    platform.recalibrate_guide("battery1", 12.0, 12.0)
    report = platform.push(oven="oven01", guide_measured_mm=12.0, group="battery1")
    assert report["ok"] is True
    assert report["guide_gate"]["calibration_version"] == 1


def test_history_review_recomputes_basis_and_is_consistent(platform):
    comp = platform.guide_compensation
    for index in range(6):
        comp.observe_alignment("battery1", f"oven{index}", 0.0, 4.0 + index)
    platform.recalibrate_guide("battery1", 9.0, 9.0)
    for index in range(3):
        comp.observe_alignment("battery1", f"oven{index}", 9.0, 9.5)

    review = comp.review("battery1")
    assert review["consistent"] is True
    assert review["mismatches"] == []
    assert len(review["migrations"]) == 1
    versions = {item["calibration_version"] for item in review["checks"]}
    # 旧版本结论原样保留、可复查；新版本结论独立编号。
    assert versions == {0, 1}
    for item in review["checks"]:
        assert item["recorded_mean_mm"] == pytest.approx(item["recomputed_mean_mm"], abs=0.01)
        assert item["recorded_samples"] == item["recomputed_samples"]


def test_conclusion_round_trips_through_journal(platform):
    comp = platform.guide_compensation
    issued = comp.observe_alignment("battery1", "oven07", 0.0, 3.0)
    loaded = comp.conclusions("battery1")[0]
    assert isinstance(loaded, CompensationConclusion)
    assert loaded.conclusion_id == issued.conclusion_id
    assert loaded.as_dict()["group"] == "battery1"


def test_audit_and_compensation_journal_share_conclusion_id(platform):
    comp = platform.guide_compensation
    conclusion = comp.observe_alignment("battery1", "oven01", 0.0, 4.0)
    entries = platform.audit.entries(limit=50)
    audit = next(item for item in entries if item["payload"].get("conclusion_id") == conclusion.conclusion_id)
    assert audit["component"] == "guide"
    assert audit["payload"]["calibration_version"] == conclusion.calibration_version
    journal_ids = {
        item["conclusion_id"]
        for item in comp.history("battery1")
        if item.get("kind", "conclusion") == "conclusion"
    }
    assert conclusion.conclusion_id in journal_ids


def test_compensation_state_persists_across_restart(tmp_path, clock):
    storage = str(tmp_path / "state")
    first = build_platform(default_config(), clock=clock, storage_root=storage)
    first.start()
    for index in range(4):
        first.guide_compensation.observe_alignment("battery1", f"oven{index}", 0.0, 12.0)
    assert first.guide_compensation.is_blocked("battery1")

    second = build_platform(default_config(), clock=clock, storage_root=storage)
    assert second.guide_compensation.is_blocked("battery1")
    assert second.guide_drift_blocked_groups() == ["battery1"]
    assert second.guide_compensation.review("battery1")["consistent"] is True


def test_unknown_group_is_rejected_not_fallback(platform):
    with pytest.raises(CokeError):
        platform.push(oven="oven01", guide_measured_mm=0.0, group="battery9")
    with pytest.raises(CokeError):
        platform.recalibrate_guide("battery9", 0.0, 0.0)


def test_console_endpoints_expose_compensation(platform):
    from coke.console.api import Api

    api = Api(platform)
    platform.guide_compensation.observe_alignment("battery1", "oven01", 0.0, 3.0)
    status, payload = api.handle("GET", "/api/guide-compensation", {}, {})
    assert status == 200 and "battery1" in payload["groups"]
    status, payload = api.handle("GET", "/api/guide-compensation/review", {"group": "battery1"}, {})
    assert status == 200 and payload["consistent"] is True
    status, payload = api.handle("GET", "/api/guide-compensation/history", {"group": "battery1"}, {})
    assert status == 200 and len(payload["records"]) >= 1
    status, payload = api.handle(
        "POST", "/api/guide-recalibrate", {},
        {"group": "battery1", "nominal_target_mm": 3.0, "measured_mm": 3.0},
    )
    assert status == 200 and payload["version_to"] == 1
    status, payload = api.handle("GET", "/api/guide-compensation/history", {"group": "missing"}, {})
    assert status == 409 and payload["component"] == "compensation"


def test_readiness_reports_drift_blocked_groups(platform):
    from coke.console.api import Api

    api = Api(platform)
    _drift_to_limit(platform.guide_compensation, "battery1", 12.0)
    status, payload = api.handle("GET", "/api/readiness", {}, {})
    assert status == 200
    assert "battery1" in payload["info"]["guide_drift_blocked_groups"]


def test_lock_outcome_is_recorded(platform):
    platform.push(oven="oven01", guide_measured_mm=0.0, group="battery1")
    outcomes = [
        item for item in platform.guide_compensation.history("battery1")
        if item.get("kind") == "lock_outcome"
    ]
    assert outcomes and outcomes[-1]["locked"] is True
