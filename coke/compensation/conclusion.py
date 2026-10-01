"""补偿结论。

每做一次对位核对，补偿器就给出一条不可变结论：用的是哪个标定版本、依据了
窗口内哪几条偏差、算出来的漂移量、本炉组的超限策略、是否停机、是否实际
更新了补偿偏移。结论同时进补偿流水（领域真相）和审计流水（巡检入口），
两份记录用同一个 ``conclusion_id`` 串联，事后能逐字段核对一致。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .drift import DriftStats


@dataclass(frozen=True)
class CompensationConclusion:
    """一次导焦栅漂移核对的结论。"""

    conclusion_id: str
    ts: float
    group: str
    oven: str
    calibration_version: int
    nominal_target_mm: float
    effective_target_mm: float
    measured_mm: float
    follow_error_mm: float
    residual_mm: float
    locked: bool | None
    stats: DriftStats
    offset_before_mm: float
    offset_after_mm: float
    offset_updated: bool
    over_limit: bool
    hard_cap_exceeded: bool
    policy: str
    action: str
    severity: str
    blocked: bool
    reason: str

    def as_dict(self) -> dict:
        return {
            "conclusion_id": self.conclusion_id,
            "ts": round(self.ts, 3),
            "group": self.group,
            "oven": self.oven,
            "calibration_version": self.calibration_version,
            "nominal_target_mm": round(self.nominal_target_mm, 2),
            "effective_target_mm": round(self.effective_target_mm, 2),
            "measured_mm": round(self.measured_mm, 2),
            "follow_error_mm": round(self.follow_error_mm, 3),
            "residual_mm": round(self.residual_mm, 3),
            "locked": self.locked,
            "stats": self.stats.as_dict(),
            "offset_before_mm": round(self.offset_before_mm, 2),
            "offset_after_mm": round(self.offset_after_mm, 2),
            "offset_updated": self.offset_updated,
            "over_limit": self.over_limit,
            "hard_cap_exceeded": self.hard_cap_exceeded,
            "policy": self.policy,
            "action": self.action,
            "severity": self.severity,
            "blocked": self.blocked,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "CompensationConclusion":
        """从落盘字段还原一条结论（历史复查用）。"""

        stats_raw = dict(raw.get("stats", {}))
        stats = DriftStats(
            samples=int(stats_raw.get("samples", 0)),
            mean_mm=float(stats_raw.get("mean_mm", 0.0)),
            slope_mm_per_sample=float(stats_raw.get("slope_mm_per_sample", 0.0)),
            latest_mm=float(stats_raw.get("latest_mm", 0.0)),
            abs_mean_mm=float(stats_raw.get("abs_mean_mm", 0.0)),
        )
        locked = raw.get("locked")
        return cls(
            conclusion_id=str(raw.get("conclusion_id", "")),
            ts=float(raw.get("ts", 0.0)),
            group=str(raw.get("group", "")),
            oven=str(raw.get("oven", "")),
            calibration_version=int(raw.get("calibration_version", 0)),
            nominal_target_mm=float(raw.get("nominal_target_mm", 0.0)),
            effective_target_mm=float(raw.get("effective_target_mm", 0.0)),
            measured_mm=float(raw.get("measured_mm", 0.0)),
            follow_error_mm=float(raw.get("follow_error_mm", raw.get("raw_deviation_mm", 0.0))),
            residual_mm=float(raw.get("residual_mm", 0.0)),
            locked=None if locked is None else bool(locked),
            stats=stats,
            offset_before_mm=float(raw.get("offset_before_mm", 0.0)),
            offset_after_mm=float(raw.get("offset_after_mm", 0.0)),
            offset_updated=bool(raw.get("offset_updated", False)),
            over_limit=bool(raw.get("over_limit", False)),
            hard_cap_exceeded=bool(raw.get("hard_cap_exceeded", False)),
            policy=str(raw.get("policy", "")),
            action=str(raw.get("action", "")),
            severity=str(raw.get("severity", "notice")),
            blocked=bool(raw.get("blocked", False)),
            reason=str(raw.get("reason", "")),
        )

    def audit_payload(self) -> dict:
        """展平成审计事件的 payload（只放标量，审计记录不嵌套大对象）。"""

        return {
            "conclusion_id": self.conclusion_id,
            "group": self.group,
            "calibration_version": self.calibration_version,
            "nominal_target_mm": round(self.nominal_target_mm, 2),
            "effective_target_mm": round(self.effective_target_mm, 2),
            "follow_error_mm": round(self.follow_error_mm, 3),
            "residual_mm": round(self.residual_mm, 3),
            "drift_mean_mm": round(self.stats.mean_mm, 3),
            "drift_slope_mm_per_sample": round(self.stats.slope_mm_per_sample, 4),
            "drift_samples": self.stats.samples,
            "offset_before_mm": round(self.offset_before_mm, 2),
            "offset_after_mm": round(self.offset_after_mm, 2),
            "offset_updated": self.offset_updated,
            "over_limit": self.over_limit,
            "hard_cap_exceeded": self.hard_cap_exceeded,
            "policy": self.policy,
            "blocked": self.blocked,
            "oven": self.oven,
        }


@dataclass(frozen=True)
class MigrationRecord:
    """一次标定基线换代（版本迁移）。"""

    migration_id: str
    ts: float
    group: str
    version_from: int
    version_to: int
    nominal_target_mm: float
    measured_mm: float
    offset_reset_mm: float
    reason: str

    def as_dict(self) -> dict:
        return {
            "kind": "migration",
            "migration_id": self.migration_id,
            "ts": round(self.ts, 3),
            "group": self.group,
            "version_from": self.version_from,
            "version_to": self.version_to,
            "nominal_target_mm": round(self.nominal_target_mm, 2),
            "measured_mm": round(self.measured_mm, 2),
            "offset_reset_mm": round(self.offset_reset_mm, 2),
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "MigrationRecord":
        return cls(
            migration_id=str(raw.get("migration_id", "")),
            ts=float(raw.get("ts", 0.0)),
            group=str(raw.get("group", "")),
            version_from=int(raw.get("version_from", 0)),
            version_to=int(raw.get("version_to", 0)),
            nominal_target_mm=float(raw.get("nominal_target_mm", 0.0)),
            measured_mm=float(raw.get("measured_mm", 0.0)),
            offset_reset_mm=float(raw.get("offset_reset_mm", 0.0)),
            reason=str(raw.get("reason", "")),
        )
