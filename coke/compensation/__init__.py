"""导焦栅漂移补偿。

导焦栅长期用固定目标位，机械磨损让对位偏差一次比一次大，偶尔锁不上栅。
本子系统按**炉组**（粒度在代码里定死，不支持全厂统一）维护带版本的标定
基线，依据历次对位偏差的滑动窗口统计自动补偿目标位；漂移超限后的处置按
炉组策略决定（继续补偿或停机要求重新标定），标定基线换代属于版本迁移。
每次补偿的依据、标定版本与审计记录用同一结论 ID 串联，可交叉核对、可
复查历史结论。
"""

from __future__ import annotations

from .baseline import CalibrationBaseline
from .compensator import GuideDriftCompensator
from .conclusion import CompensationConclusion, MigrationRecord
from .drift import DriftStats, drift_stats
from .policy import (
    COMPENSATION_SCOPES,
    POLICY_COMPENSATE,
    POLICY_RECALIBRATE,
    SCOPE_BATTERY_GROUP,
)

__all__ = [
    "GuideDriftCompensator",
    "CalibrationBaseline",
    "CompensationConclusion",
    "MigrationRecord",
    "DriftStats",
    "drift_stats",
    "SCOPE_BATTERY_GROUP",
    "COMPENSATION_SCOPES",
    "POLICY_COMPENSATE",
    "POLICY_RECALIBRATE",
]
