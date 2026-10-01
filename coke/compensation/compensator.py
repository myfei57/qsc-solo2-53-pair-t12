"""导焦栅漂移补偿器（按炉组独立计算）。

每个炉组各自持有一条"标定基线版本 + 学习偏移 + 跟随误差窗口 + 停机标志"，
互不相借：A 炉组磨出的偏移不会摊到 B 炉组。粒度在 :mod:`policy` 里定死。

物理模型：导焦栅收到"去有效目标位"的指令，因磨损会系统性过冲/欠冲
``d``，实测停在 ``effective + d``。补偿就是学习这个 ``d`` 并把目标位朝
反方向挪：

    effective = nominal + offset， offset 朝 −mean(follow_error) 收敛
    follow_error = measured − effective   （执行器相对指令的跟随误差，入窗）
    residual     = measured − nominal     （相对名义位的对位残差，决定能否锁栅）

收敛后 effective = nominal − d，实测正好停在 nominal，残差为 0。

一次对位核对（:meth:`observe_alignment`）的处置按顺序是：

1. 取该炉组当前标定版本下的名义目标位与累计偏移，算出实际下发的有效
   目标位，跟随误差入窗（窗口随标定版本重置）；
2. 对窗口做漂移统计（均值 + 趋势斜率）；
3. 若该炉组已被停机（漂移超限且策略为 ``recalibrate``，或补偿量触物理
   硬顶），保持冻结、继续要求重新标定；
4. 否则判断漂移是否超限：策略为 ``compensate`` 的炉组继续更新偏移但持续
   告警；策略为 ``recalibrate`` 的炉组冻结偏移并禁止锁栅；
5. 不论哪种结果都落一条 :class:`CompensationConclusion`（补偿流水 + 审计）。

重新标定（:meth:`recalibrate`）让基线换代：版本加一、偏移归零、窗口清空、
解除停机，并额外写一条 :class:`MigrationRecord`。旧版本结论只追加不修改。
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass, field
from typing import Deque

from ..clock import Clock
from ..errors import ConfigError
from ..eventbus import Event, EventBus
from ..ids import IdFactory
from ..store.jsonfile import JsonStore
from ..store.journal import JsonlJournal
from .baseline import CalibrationBaseline
from .conclusion import CompensationConclusion, MigrationRecord
from .drift import drift_stats
from .policy import (
    ACTION_BASELINE_MIGRATED,
    ACTION_COMPENSATED,
    ACTION_COMPENSATED_OVER_LIMIT,
    ACTION_HOLD,
    ACTION_MONITOR,
    ACTION_RECALIBRATION_REQUIRED,
    POLICY_COMPENSATE,
    POLICY_RECALIBRATE,
    SCOPE_BATTERY_GROUP,
)

_ACTIVE_DOC_PREFIX = "guide_compensation_active"


def _safe(group: str) -> str:
    return "".join(ch for ch in group if ch.isalnum() or ch in ("-", "_")) or "group"


def _doc_id(group: str) -> str:
    return f"{_ACTIVE_DOC_PREFIX}_{_safe(group)}"


@dataclass
class _GroupState:
    """单个炉组的运行态（仅保存在进程内／当前态文档）。"""

    group: str
    policy: str
    baseline: CalibrationBaseline
    offset_mm: float = 0.0
    blocked: bool = False
    block_reason: str = ""
    deviations: Deque[float] = field(default_factory=deque)
    conclusions: int = 0

    def as_dict(self) -> dict:
        return {
            "group": self.group,
            "policy": self.policy,
            "baseline": self.baseline.as_dict(),
            "offset_mm": round(self.offset_mm, 3),
            "blocked": self.blocked,
            "block_reason": self.block_reason,
            "deviations": [round(value, 3) for value in self.deviations],
            "conclusions": self.conclusions,
        }

    @classmethod
    def from_dict(cls, raw: dict, window: int) -> "_GroupState":
        deviations = deque((float(value) for value in raw.get("deviations", ())), maxlen=window)
        return cls(
            group=str(raw["group"]),
            policy=str(raw.get("policy", POLICY_RECALIBRATE)),
            baseline=CalibrationBaseline.from_dict(raw["baseline"]),
            offset_mm=float(raw.get("offset_mm", 0.0)),
            blocked=bool(raw.get("blocked", False)),
            block_reason=str(raw.get("block_reason", "")),
            deviations=deviations,
            conclusions=int(raw.get("conclusions", 0)),
        )


class GuideDriftCompensator:
    """按炉组独立维护标定基线与漂移补偿。"""

    def __init__(
        self,
        config,
        clock: Clock,
        bus: EventBus,
        store: JsonStore,
        *,
        segment_limit: int = 512,
    ) -> None:
        if config.scope != SCOPE_BATTERY_GROUP:
            # 补偿粒度定死按炉组；全厂统一口径刻意不实现。
            raise ConfigError(
                f"导焦栅补偿粒度仅支持 {SCOPE_BATTERY_GROUP!r}，收到 {config.scope!r}",
                component="config",
            )
        self._config = config
        self._clock = clock
        self._bus = bus
        self._store = store
        self._ids = IdFactory("gcomp")
        self._lock = threading.RLock()
        self._journals: dict[str, JsonlJournal] = {}
        self._states: dict[str, _GroupState] = {}
        now = clock.now()
        for group, policy in config.group_policies.items():
            journal = JsonlJournal(
                store.root, f"guide_compensation_{_safe(group)}", segment_limit=segment_limit
            )
            self._journals[group] = journal
            state = self._load_state(group, policy, now)
            self._states[group] = state

    # ------------------------------------------------------------------ 装配

    def _load_state(self, group: str, policy: str, now: float) -> _GroupState:
        """优先从当前态文档恢复；没有则用配置里的名义目标位起一份基线。"""

        raw = self._store.read(_doc_id(group))
        if isinstance(raw, dict) and isinstance(raw.get("state"), dict):
            state = _GroupState.from_dict(raw["state"], self._config.sample_window)
            # 超限策略是运行策略，以当前配置为准；标定基线（名义目标 + 版本）
            # 是标定数据，沿用落盘值，配置里的名义目标只用于首次播种。
            state.policy = policy
            return state
        nominal = self._config.nominal_target_for(group)
        baseline = CalibrationBaseline(group=group, nominal_target_mm=nominal, version=0, calibrated_ts=now)
        return _GroupState(group=group, policy=policy, baseline=baseline)

    def _persist_state(self, state: _GroupState) -> None:
        self._store.write(
            _doc_id(state.group),
            {"scope": SCOPE_BATTERY_GROUP, "state": state.as_dict()},
        )

    @property
    def scope(self) -> str:
        return SCOPE_BATTERY_GROUP

    def groups(self) -> list[str]:
        """返回受补偿的炉组（排序后）。"""

        with self._lock:
            return sorted(self._states)

    def has_group(self, group: str) -> bool:
        with self._lock:
            return group in self._states

    # ------------------------------------------------------------------ 查询

    def baseline(self, group: str) -> CalibrationBaseline:
        with self._lock:
            return self._require(group).baseline

    def effective_target_mm(self, group: str, nominal_target_mm: float | None = None) -> float:
        """返回某炉组当前应下发的目标位（名义目标 + 学习偏移）。

        停机时不改变有效目标——停机的含义是不再学习、并在对位后拒绝锁栅，
        而不是把目标位偷偷推到未知位置。
        """

        with self._lock:
            state = self._require(group)
            nominal = state.baseline.nominal_target_mm if nominal_target_mm is None else nominal_target_mm
            return round(float(nominal) + state.offset_mm, 2)

    def is_blocked(self, group: str) -> bool:
        with self._lock:
            return self._require(group).blocked

    def offset_mm(self, group: str) -> float:
        with self._lock:
            return self._require(group).offset_mm

    def group_summary(self, group: str) -> dict:
        """单炉组补偿状态摘要（供控制台展示）。"""

        with self._lock:
            state = self._require(group)
            stats = drift_stats(list(state.deviations))
            journal = self._journals[group]
            return {
                "group": group,
                "scope": SCOPE_BATTERY_GROUP,
                "policy": state.policy,
                "nominal_target_mm": state.baseline.nominal_target_mm,
                "effective_target_mm": round(state.baseline.nominal_target_mm + state.offset_mm, 2),
                "offset_mm": round(state.offset_mm, 3),
                "calibration_version": state.baseline.version,
                "calibrated_ts": round(state.baseline.calibrated_ts, 3),
                "blocked": state.blocked,
                "block_reason": state.block_reason,
                "drift_limit_mm": self._config.drift_limit_mm,
                "max_correction_mm": self._config.max_correction_mm,
                "max_step_mm": self._config.max_step_mm,
                "stats": stats.as_dict(),
                "conclusions": state.conclusions,
                "journal_count": journal.count(),
            }

    def summary(self) -> dict:
        """全部炉组摘要。"""

        with self._lock:
            return {
                "scope": SCOPE_BATTERY_GROUP,
                "plant_wide_supported": False,
                "sample_window": self._config.sample_window,
                "min_samples": self._config.min_samples,
                "deadband_mm": self._config.deadband_mm,
                "groups": {group: self.group_summary(group) for group in self.groups()},
            }

    # ------------------------------------------------------------- 对位核对

    def observe_alignment(
        self,
        group: str,
        oven: str,
        nominal_target_mm: float,
        measured_mm: float,
        *,
        locked: bool | None = None,
    ) -> CompensationConclusion:
        """记录一次对位偏差并给出补偿结论。

        ``nominal_target_mm`` 为该炉组当前标定版本的名义目标；``measured_mm``
        为导焦栅实测停留位置；``locked`` 可带本次是否成功锁栅的现场反馈。
        """

        with self._lock:
            state = self._require(group)
            ts = self._clock.now()
            conclusion_id = self._ids.next()
            offset_before = state.offset_mm
            effective = round(float(nominal_target_mm) + state.offset_mm, 2)
            # 导焦栅收到的是含补偿的 effective；执行器相对指令的跟随误差才是
            # 这一炉真正新观测到的系统性漂移，用它入窗。
            follow_error = round(float(measured_mm) - effective, 3)
            state.deviations.append(follow_error)
            stats = drift_stats(list(state.deviations))
            # 对位残差核对名义位（补偿想消除的就是它），决定能否锁栅。
            residual = round(float(measured_mm) - float(nominal_target_mm), 3)

            enough = stats.samples >= max(2, self._config.min_samples)
            over_limit = enough and stats.abs_mean_mm > self._config.drift_limit_mm
            # 物理需要的补偿量是窗口均值；它本身超出累计上限，说明靠补偿已经
            # 追不回磨损，任何超限策略都必须停下来重新标定。
            hard_cap = enough and abs(stats.mean_mm) > self._config.max_correction_mm + 1e-9
            # 偏移朝漂移的反方向修正：实测停在名义位正侧，就把目标位往负侧挪。
            desired_offset = -stats.mean_mm
            candidate = (
                self._next_offset(state.offset_mm, desired_offset) if enough and not hard_cap else None
            )

            offset_after = offset_before
            updated = False
            if state.blocked:
                # 已被停机：冻结补偿，直到人工重新标定解除。
                action = ACTION_RECALIBRATION_REQUIRED
                severity = "alarm"
                blocked = True
                reason = state.block_reason or "等待重新标定"
            elif hard_cap:
                # 需要的累计补偿超过机械允许范围，任何策略都必须停机。
                state.blocked = True
                state.block_reason = (
                    f"所需补偿 {stats.mean_mm:.2f}mm 超过累计上限 "
                    f"{self._config.max_correction_mm:.2f}mm"
                )
                action = ACTION_RECALIBRATION_REQUIRED
                severity = "alarm"
                blocked = True
                reason = state.block_reason
            elif over_limit and state.policy == POLICY_RECALIBRATE:
                state.blocked = True
                state.block_reason = (
                    f"漂移均值 {stats.abs_mean_mm:.2f}mm 超过漂移限值 "
                    f"{self._config.drift_limit_mm:.2f}mm，炉组策略要求重新标定"
                )
                action = ACTION_RECALIBRATION_REQUIRED
                severity = "alarm"
                blocked = True
                reason = state.block_reason
            else:
                blocked = False
                if candidate is not None and abs(stats.mean_mm) > self._config.deadband_mm:
                    offset_after = candidate
                    updated = abs(offset_after - offset_before) > 1e-9
                if over_limit:
                    action = ACTION_COMPENSATED_OVER_LIMIT
                    severity = "warning"
                    reason = (
                        f"漂移均值 {stats.abs_mean_mm:.2f}mm 超限，"
                        f"炉组策略 {POLICY_COMPENSATE}：继续自动补偿"
                    )
                elif updated:
                    action = ACTION_COMPENSATED
                    severity = "notice"
                    reason = "漂移超过死区，更新补偿偏移"
                elif enough:
                    action = ACTION_HOLD
                    severity = "notice"
                    reason = "漂移在死区内，维持当前偏移"
                else:
                    action = ACTION_MONITOR
                    severity = "notice"
                    reason = f"样本不足（{stats.samples}/{self._config.min_samples}），仅观测"
                state.offset_mm = offset_after

            conclusion = CompensationConclusion(
                conclusion_id=conclusion_id,
                ts=ts,
                group=group,
                oven=oven,
                calibration_version=state.baseline.version,
                nominal_target_mm=float(nominal_target_mm),
                effective_target_mm=effective,
                measured_mm=float(measured_mm),
                follow_error_mm=follow_error,
                residual_mm=residual,
                locked=locked,
                stats=stats,
                offset_before_mm=offset_before,
                offset_after_mm=offset_after,
                offset_updated=updated,
                over_limit=bool(over_limit),
                hard_cap_exceeded=bool(hard_cap),
                policy=state.policy,
                action=action,
                severity=severity,
                blocked=blocked,
                reason=reason,
            )
            state.conclusions += 1
            self._journals[group].append(conclusion.as_dict())
            self._persist_state(state)
            self._bus.publish(
                Event.of(
                    "guide",
                    "guide",
                    action,
                    severity=severity,
                    message=reason,
                    **conclusion.audit_payload(),
                )
            )
            return conclusion

    def gate_alignment(
        self,
        group: str,
        oven: str,
        nominal_target_mm: float,
        measured_mm: float,
        tolerance_mm: float,
        *,
        locked: bool | None = None,
    ) -> dict:
        """对位决策门：核对 + 补偿 + 是否放行锁栅。

        返回有效目标位、是否在公差内、是否被重新标定联锁拦下以及完整结论。
        推焦编排锁定导焦栅之前必须经过这道门。
        """

        conclusion = self.observe_alignment(
            group,
            oven,
            nominal_target_mm,
            measured_mm,
            locked=locked,
        )
        aligned = abs(conclusion.residual_mm) <= tolerance_mm and not conclusion.blocked
        return {
            "group": group,
            "oven": oven,
            "nominal_target_mm": conclusion.nominal_target_mm,
            "effective_target_mm": conclusion.effective_target_mm,
            "measured_mm": conclusion.measured_mm,
            "residual_mm": conclusion.residual_mm,
            "tolerance_mm": float(tolerance_mm),
            "aligned": bool(aligned),
            "blocked": conclusion.blocked,
            "reason": conclusion.reason,
            "calibration_version": conclusion.calibration_version,
            "conclusion_id": conclusion.conclusion_id,
            "conclusion": conclusion,
        }

    # ------------------------------------------------------------- 锁栅反馈

    def record_lock_outcome(self, group: str, oven: str, locked: bool, detail: str = "") -> dict:
        """记录本炉组本次对位后导焦栅是否真的锁上。

        "偶尔锁不上"是磨损的现场表现之一：即便偏差在公差内，锁栅失败也要
        作为 warning 进补偿流水和审计，供巡检把"锁不上"与漂移趋势对上。
        该记录不改学习偏移，只留痕。
        """

        with self._lock:
            state = self._require(group)
            record = {
                "kind": "lock_outcome",
                "conclusion_id": self._ids.next(),
                "ts": round(self._clock.now(), 3),
                "group": group,
                "oven": oven,
                "calibration_version": state.baseline.version,
                "offset_mm": round(state.offset_mm, 3),
                "locked": bool(locked),
                "detail": detail,
            }
            self._journals[group].append(record)
            if not locked:
                self._bus.publish(
                    Event.of(
                        "guide",
                        "guide",
                        "guide_apron_lock_failed",
                        severity="warning",
                        message=detail or f"炉组 {group} 导焦栅锁栅失败，需关注磨损漂移",
                        **{key: value for key, value in record.items() if key not in ("kind", "ts")},
                    )
                )
            return record

    # ------------------------------------------------------------- 重新标定

    def recalibrate(
        self,
        group: str,
        nominal_target_mm: float,
        measured_mm: float,
        *,
        reason: str = "人工重新标定",
    ) -> dict:
        """标定基线换代（版本迁移）：版本加一、偏移归零、窗口清空、解除停机。"""

        with self._lock:
            state = self._require(group)
            ts = self._clock.now()
            before = state.baseline
            offset_reset = state.offset_mm
            new_baseline = before.migrated(nominal_target_mm, ts)
            state.baseline = new_baseline
            state.offset_mm = 0.0
            state.deviations.clear()
            state.blocked = False
            state.block_reason = ""

            migration = MigrationRecord(
                migration_id=self._ids.next(),
                ts=ts,
                group=group,
                version_from=before.version,
                version_to=new_baseline.version,
                nominal_target_mm=float(nominal_target_mm),
                measured_mm=float(measured_mm),
                offset_reset_mm=offset_reset,
                reason=reason,
            )
            self._journals[group].append(migration.as_dict())
            self._persist_state(state)
            self._bus.publish(
                Event.of(
                    "guide",
                    "guide",
                    ACTION_BASELINE_MIGRATED,
                    severity="notice",
                    message=f"炉组 {group} 导焦栅标定基线换代：v{before.version} → v{new_baseline.version}",
                    migration_id=migration.migration_id,
                    group=group,
                    version_from=before.version,
                    version_to=new_baseline.version,
                    nominal_target_mm=round(new_baseline.nominal_target_mm, 2),
                    measured_mm=round(float(measured_mm), 2),
                    offset_reset_mm=round(offset_reset, 2),
                    reason=reason,
                )
            )
            return {
                "group": group,
                "migration_id": migration.migration_id,
                "version_from": before.version,
                "version_to": new_baseline.version,
                "nominal_target_mm": round(new_baseline.nominal_target_mm, 2),
                "measured_mm": round(float(measured_mm), 2),
                "offset_reset_mm": round(offset_reset, 2),
                "blocked": False,
            }

    # ------------------------------------------------------------- 历史复查

    def history(self, group: str, limit: int = 200) -> list[dict]:
        """读取某炉组补偿流水原始记录（结论与迁移混排，只追加）。"""

        with self._lock:
            self._require(group)
            return self._journals[group].read_all()[-limit:]

    def conclusions(self, group: str, limit: int = 200) -> list[CompensationConclusion]:
        """读取某炉组的历史补偿结论（排除迁移与锁栅反馈记录）。"""

        return [
            CompensationConclusion.from_dict(item)
            for item in self.history(group, limit=limit)
            if item.get("kind", "conclusion") == "conclusion"
        ]

    def migrations(self, group: str) -> list[MigrationRecord]:
        """读取某炉组的全部基线换代记录。"""

        return [
            MigrationRecord.from_dict(item)
            for item in self.history(group, limit=100_000)
            if item.get("kind") == "migration"
        ]

    def review(self, group: str, limit: int = 200) -> dict:
        """历史复查：重算每条结论的依据统计并核对版本与字段一致性。

        对账规则：

        * 结论内 ``effective = nominal + offset_before`` 必须成立；
        * ``follow_error = measured − effective``（入窗依据）；
        * ``residual = measured − nominal``（锁栅核对依据）；
        * 重放窗口重算的跟随误差均值、样本数必须与结论记录一致。
        """

        with self._lock:
            self._require(group)
            raw = self._journals[group].read_all()[-limit:]

        # 按版本重放该版本下的跟随误差，重算每条结论当时应有的窗口统计。
        windows: dict[int, list[float]] = {}
        migrations: list[dict] = []
        lock_outcomes: list[dict] = []
        checks: list[dict] = []
        mismatches: list[dict] = []

        for item in raw:
            kind = item.get("kind", "conclusion")
            if kind == "migration":
                migrations.append(MigrationRecord.from_dict(item).as_dict())
                windows.pop(int(item.get("version_to", 0)), None)
                continue
            if kind == "lock_outcome":
                lock_outcomes.append(item)
                continue
            conclusion = CompensationConclusion.from_dict(item)
            window = windows.setdefault(conclusion.calibration_version, [])
            window.append(conclusion.follow_error_mm)
            recomputed = drift_stats(list(window))

            problems: list[str] = []
            expected_effective = round(
                conclusion.nominal_target_mm + conclusion.offset_before_mm, 2
            )
            if abs(expected_effective - conclusion.effective_target_mm) > 0.01:
                problems.append("effective_target_mismatch")
            expected_follow = round(
                conclusion.measured_mm - conclusion.effective_target_mm, 3
            )
            if abs(expected_follow - conclusion.follow_error_mm) > 0.005:
                problems.append("follow_error_mismatch")
            expected_residual = round(
                conclusion.measured_mm - conclusion.nominal_target_mm, 3
            )
            if abs(expected_residual - conclusion.residual_mm) > 0.005:
                problems.append("residual_mismatch")
            if abs(recomputed.mean_mm - conclusion.stats.mean_mm) > 0.01:
                problems.append("drift_basis_mismatch")
            if recomputed.samples != conclusion.stats.samples:
                problems.append("sample_count_mismatch")

            checked = {
                "conclusion_id": conclusion.conclusion_id,
                "ts": round(conclusion.ts, 3),
                "oven": conclusion.oven,
                "calibration_version": conclusion.calibration_version,
                "action": conclusion.action,
                "recorded_mean_mm": conclusion.stats.mean_mm,
                "recomputed_mean_mm": recomputed.mean_mm,
                "recorded_samples": conclusion.stats.samples,
                "recomputed_samples": recomputed.samples,
                "offset_before_mm": conclusion.offset_before_mm,
                "offset_after_mm": conclusion.offset_after_mm,
                "blocked": conclusion.blocked,
                "problems": problems,
                "ok": not problems,
            }
            checks.append(checked)
            if problems:
                mismatches.append(checked)

        return {
            "group": group,
            "scope": SCOPE_BATTERY_GROUP,
            "records_reviewed": len(checks) + len(migrations) + len(lock_outcomes),
            "conclusions_reviewed": len(checks),
            "migrations": migrations,
            "lock_outcomes": lock_outcomes,
            "checks": checks,
            "mismatches": mismatches,
            "consistent": not mismatches,
        }

    # ------------------------------------------------------------------ 内部

    def _require(self, group: str) -> _GroupState:
        try:
            return self._states[group]
        except KeyError as error:
            raise ConfigError(
                f"炉组 {group!r} 未登记导焦栅补偿（受补偿炉组：{sorted(self._states)}）",
                component="compensation",
            ) from error

    def _next_offset(self, current_mm: float, desired_mm: float) -> float:
        """根据漂移均值算下一版偏移：单次调整不超过步长，累计不超过硬顶。"""

        delta = desired_mm - current_mm
        step = max(-self._config.max_step_mm, min(self._config.max_step_mm, delta))
        candidate = current_mm + step
        return round(
            max(-self._config.max_correction_mm, min(self._config.max_correction_mm, candidate)),
            2,
        )
