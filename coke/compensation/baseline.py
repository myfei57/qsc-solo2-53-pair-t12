"""导焦栅标定基线（带版本）。

基线是"标定那一刻导焦栅对准炭化室的名义目标位"。机械磨损后实测位置持续
漂移，补偿器在基线之上叠加一个可学习的偏移；当漂移大到需要重新标定时，
人工以一次实测对位给出新的名义目标位，基线换代——**版本号加一，这是一次
迁移（migration）**：新版本从零重新学习补偿，旧版本的全部结论仍归属旧
版本号保留，供复查，不允许把旧偏移带进新版本。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CalibrationBaseline:
    """某个炉组导焦栅的标定基线版本。"""

    group: str
    nominal_target_mm: float
    version: int
    calibrated_ts: float

    def as_dict(self) -> dict:
        return {
            "group": self.group,
            "nominal_target_mm": round(self.nominal_target_mm, 2),
            "version": self.version,
            "calibrated_ts": round(self.calibrated_ts, 3),
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "CalibrationBaseline":
        return cls(
            group=str(raw["group"]),
            nominal_target_mm=float(raw.get("nominal_target_mm", 0.0)),
            version=int(raw.get("version", 0)),
            calibrated_ts=float(raw.get("calibrated_ts", 0.0)),
        )

    def migrated(self, nominal_target_mm: float, ts: float) -> "CalibrationBaseline":
        """生成下一代基线（版本加一）。"""

        return CalibrationBaseline(
            group=self.group,
            nominal_target_mm=float(nominal_target_mm),
            version=self.version + 1,
            calibrated_ts=ts,
        )
