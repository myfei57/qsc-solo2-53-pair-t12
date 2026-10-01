"""导焦栅对位漂移补偿。

导焦栅长期对着固定目标位对位，机械磨损会让实际停位一点点漂移，偏差累积到
超差就锁不上栅。本模块按 **炉组** 维护一个补偿器——补偿口径定死为按炉组单
独计算，不做全厂统一：漂移来自拦焦车本体与走行轨的磨损，是炉组级设备的局
部特性，全厂统一估计会把一台车的磨损摊到别的炉组上。

工作口径：

- 每次对位后把残差（实测 - 补偿后目标）记入窗口，窗口均值即漂移估计；
- 补偿量 = -漂移，按 ``max_offset_mm`` 限幅后叠加到下一次目标位上；
- 漂移达到 ``recalibrate_threshold_mm`` 视为超限并报警；超限后是继续补偿
  还是停机待标定，由炉组策略（``continue`` / ``halt``）决定，``halt`` 一
  旦触发就锁存，直到重新标定才解除；
- 补偿基线带版本（epoch），重新标定即版本换代，按迁移处理：旧补偿量与窗
  口样本作废清零，历史判定保留供复查，迁移本身落审计；
- 每条补偿判定都带齐依据（窗口样本、漂移、补偿前后值、版本），落盘也落审
  计；:meth:`GuideCompensator.review` 重算判定链并与审计流水核对，历史结
  论据此可复查。
"""

from __future__ import annotations

from ..audit.record import AuditRecord
from ..config import GuideCompensationLimits
from ..errors import AlignmentError, StorageError

SCHEMA_VERSION = 2
DECISION_LIMIT = 500


def _round2(value: float) -> float:
    return round(float(value), 2)


def migrate_state(raw: object, unit: str) -> tuple[dict, list[str]]:
    """把落盘的补偿状态迁移到当前结构版本，返回 ``(状态, 迁移说明)``。

    补偿状态按炉组分文档存放，炉组不符说明拿错了别家的磨损结论，直接拒绝
    装入；结构版本过旧则逐代迁移，迁不动的抛 :class:`StorageError`。
    """

    if not isinstance(raw, dict):
        raise StorageError("补偿状态必须是映射", component="guide")
    if raw.get("unit") != unit:
        raise StorageError(
            f"补偿状态属于炉组 {raw.get('unit')!r}，不能装入炉组 {unit!r}", component="guide"
        )
    schema = int(raw.get("schema", 1))
    state = dict(raw)
    migrations: list[str] = []
    if schema < 2:
        # 结构 1 → 2：补偿量字段 compensation_mm 改名 offset_mm，
        # 补判定日志与超限锁存字段。
        state["offset_mm"] = float(state.pop("compensation_mm", 0.0))
        state.setdefault("decisions", [])
        state.setdefault("overrun", False)
        state.setdefault("halted", False)
        migrations.append("schema_1_to_2")
        schema = 2
    if schema != SCHEMA_VERSION:
        raise StorageError(
            f"补偿状态结构版本 {schema} 无法迁移到 {SCHEMA_VERSION}", component="guide"
        )
    state["schema"] = SCHEMA_VERSION
    return state, migrations


class GuideCompensator:
    """单个炉组的导焦栅对位漂移补偿器。"""

    def __init__(self, unit: str, limits: GuideCompensationLimits, baseline_mm: float = 0.0) -> None:
        self._unit = unit
        self._limits = limits
        self._policy = limits.policy_for(unit)
        self._epoch = 0
        self._baseline_mm = float(baseline_mm)
        self._offset_mm = 0.0
        self._samples: list[dict] = []
        self._decisions: list[dict] = []
        self._overrun = False
        self._halted = False

    @property
    def epoch(self) -> int:
        """返回补偿基线版本（标定代际），重新标定一次加一。"""

        return self._epoch

    @property
    def baseline_mm(self) -> float:
        """返回当前标定基线。"""

        return self._baseline_mm

    def halted(self) -> bool:
        """是否已因漂移超限停机待标定（仅 halt 策略会锁存）。"""

        return self._halted

    def plan(self, target_mm: float) -> dict:
        """按当前补偿量换算本次对位目标；停机待标定时拒绝换算。"""

        if self._halted:
            raise AlignmentError("导焦栅漂移超限，需重新标定后才能继续对位", component="guide")
        return {
            "target_mm": _round2(target_mm),
            "offset_mm": _round2(self._offset_mm),
            "commanded_mm": _round2(float(target_mm) + self._offset_mm),
            "epoch": self._epoch,
        }

    def observe(self, oven: str, plan: dict, measured_mm: float, ts: float, op_id: str = "") -> dict:
        """记录一次对位残差，重算漂移与补偿量，返回带齐依据的判定。

        判定里的窗口样本、漂移、补偿前后值全部按两位小数落账，复查时按同一
        口径重算可以逐字段对上；``op_id`` 把判定与本次推焦操作、审计流水串
        到同一条线上。
        """

        deviation = _round2(float(measured_mm) - float(plan["commanded_mm"]))
        self._samples.append({"ts": round(float(ts), 3), "oven": oven, "deviation_mm": deviation})
        if len(self._samples) > self._limits.window:
            del self._samples[: len(self._samples) - self._limits.window]
        window = [item["deviation_mm"] for item in self._samples]
        drift = _round2(sum(window) / len(window))
        clamped = abs(drift) > self._limits.max_offset_mm
        offset_after = _round2(
            max(-self._limits.max_offset_mm, min(self._limits.max_offset_mm, -drift))
        )
        overrun = abs(drift) >= self._limits.recalibrate_threshold_mm
        self._offset_mm = offset_after
        self._overrun = overrun
        if overrun and self._policy == "halt":
            self._halted = True
        decision = {
            "ts": round(float(ts), 3),
            "op_id": op_id,
            "unit": self._unit,
            "oven": oven,
            "epoch": self._epoch,
            "policy": self._policy,
            "target_mm": plan["target_mm"],
            "offset_before_mm": plan["offset_mm"],
            "commanded_mm": plan["commanded_mm"],
            "measured_mm": _round2(measured_mm),
            "deviation_mm": deviation,
            "window": list(window),
            "drift_mm": drift,
            "offset_after_mm": offset_after,
            "clamped": clamped,
            "overrun": overrun,
            "halted": self._halted,
        }
        self._decisions.append(decision)
        if len(self._decisions) > DECISION_LIMIT:
            del self._decisions[: len(self._decisions) - DECISION_LIMIT]
        return dict(decision)

    def recalibrate(self, baseline_mm: float, ts: float) -> dict:
        """重新标定补偿基线：标定版本换代，按迁移处理并返回迁移记录。

        换代后旧补偿量与窗口样本作废（它们相对旧机械基准测得，对新基准无效），
        历史判定保留，凭 epoch 区分属于哪一代的结论。
        """

        migration = {
            "ts": round(float(ts), 3),
            "from_epoch": self._epoch,
            "to_epoch": self._epoch + 1,
            "baseline_before_mm": _round2(self._baseline_mm),
            "baseline_after_mm": _round2(baseline_mm),
            "offset_reset_mm": _round2(self._offset_mm),
            "samples_cleared": len(self._samples),
            "decisions_kept": len(self._decisions),
            "overrun_cleared": self._overrun or self._halted,
        }
        self._epoch += 1
        self._baseline_mm = float(baseline_mm)
        self._offset_mm = 0.0
        self._samples = []
        self._overrun = False
        self._halted = False
        return migration

    def status(self) -> dict:
        """返回补偿状态摘要，供控制台展示。"""

        window = [item["deviation_mm"] for item in self._samples]
        drift = _round2(sum(window) / len(window)) if window else 0.0
        return {
            "unit": self._unit,
            "epoch": self._epoch,
            "baseline_mm": _round2(self._baseline_mm),
            "offset_mm": _round2(self._offset_mm),
            "drift_mm": drift,
            "samples": len(self._samples),
            "window": self._limits.window,
            "max_offset_mm": self._limits.max_offset_mm,
            "recalibrate_threshold_mm": self._limits.recalibrate_threshold_mm,
            "policy": self._policy,
            "overrun": self._overrun,
            "halted": self._halted,
            "decisions_logged": len(self._decisions),
        }

    def decisions(self, limit: int = 50) -> list[dict]:
        """返回最近的补偿判定（按时间升序），供控制台与复查使用。"""

        return [dict(item) for item in self._decisions[-max(1, limit) :]]

    def state_dict(self) -> dict:
        """导出可落盘的补偿状态。"""

        return {
            "schema": SCHEMA_VERSION,
            "unit": self._unit,
            "epoch": self._epoch,
            "baseline_mm": self._baseline_mm,
            "offset_mm": self._offset_mm,
            "samples": [dict(item) for item in self._samples],
            "decisions": [dict(item) for item in self._decisions],
            "overrun": self._overrun,
            "halted": self._halted,
        }

    def load_state(self, state: dict) -> None:
        """装入（可能已迁移过的）补偿状态。"""

        self._epoch = int(state.get("epoch", 0))
        self._baseline_mm = float(state.get("baseline_mm", 0.0))
        self._offset_mm = float(state.get("offset_mm", 0.0))
        self._samples = [dict(item) for item in state.get("samples", [])]
        if len(self._samples) > self._limits.window:
            del self._samples[: len(self._samples) - self._limits.window]
        self._decisions = [dict(item) for item in state.get("decisions", [])]
        if len(self._decisions) > DECISION_LIMIT:
            del self._decisions[: len(self._decisions) - DECISION_LIMIT]
        self._overrun = bool(state.get("overrun", False))
        self._halted = bool(state.get("halted", False))

    def review(self, audit_records: list[AuditRecord]) -> dict:
        """复查判定日志：重算每条判定，并与审计流水逐条核对。

        三层核对：判定依据可重算（窗口样本 → 漂移 → 补偿量 → 超限与锁存）、
        判定链连续（同代补偿量首尾相接、换代必清零且跳代必有迁移记录）、判
        定与审计流水一致（每条判定都能在流水中找到同版本同数值的记录）。
        """

        decisions = [dict(item) for item in self._decisions]
        mismatches: list[dict] = []
        halted_running = False
        previous: dict | None = None
        for index, decision in enumerate(decisions):
            where = f"decision[{index}]"
            window = decision.get("window") or []
            if not window:
                mismatches.append({"where": where, "issue": "判定缺少窗口样本，依据不完整"})
                previous = decision
                continue
            drift = _round2(sum(window) / len(window))
            if drift != decision["drift_mm"]:
                mismatches.append({"where": where, "issue": f"漂移重算 {drift} 与记录 {decision['drift_mm']} 不符"})
            clamped = abs(drift) > self._limits.max_offset_mm
            offset = _round2(
                max(-self._limits.max_offset_mm, min(self._limits.max_offset_mm, -drift))
            )
            if clamped != decision["clamped"]:
                mismatches.append({"where": where, "issue": "限幅标记与重算不符"})
            if offset != decision["offset_after_mm"]:
                mismatches.append(
                    {"where": where, "issue": f"补偿量重算 {offset} 与记录 {decision['offset_after_mm']} 不符"}
                )
            overrun = abs(drift) >= self._limits.recalibrate_threshold_mm
            if overrun != decision["overrun"]:
                mismatches.append({"where": where, "issue": "超限标记与重算不符"})
            if previous is not None and decision["epoch"] != previous["epoch"]:
                halted_running = False  # 换代迁移会清掉锁存
            expected_halted = halted_running or (overrun and self._policy == "halt")
            if expected_halted != decision["halted"]:
                mismatches.append({"where": where, "issue": "停机锁存状态与重算不符"})
            halted_running = expected_halted
            if previous is not None:
                if decision["epoch"] == previous["epoch"]:
                    if decision["offset_before_mm"] != previous["offset_after_mm"]:
                        mismatches.append({"where": where, "issue": "同代判定之间补偿量链断裂"})
                elif decision["epoch"] == previous["epoch"] + 1:
                    if decision["offset_before_mm"] != 0.0:
                        mismatches.append({"where": where, "issue": "换代后补偿量未清零"})
                else:
                    mismatches.append({"where": where, "issue": "标定版本回退或跳代"})
            previous = decision
        if decisions and self._epoch < decisions[-1]["epoch"]:
            mismatches.append({"where": "state", "issue": "当前标定版本早于判定日志版本"})

        compensated = [
            record
            for record in audit_records
            if record.action == "guide_compensated" and record.payload.get("unit") == self._unit
        ]
        pool = list(compensated)
        audit_missing: list[dict] = []
        for decision in decisions:
            key = (
                decision["op_id"],
                decision["epoch"],
                decision["offset_after_mm"],
                decision["deviation_mm"],
                decision["commanded_mm"],
            )
            hit = None
            for record in pool:
                payload = record.payload
                candidate = (
                    str(payload.get("op_id", "")),
                    int(payload.get("epoch", -1)),
                    float(payload.get("offset_after_mm", 0.0)),
                    float(payload.get("deviation_mm", 0.0)),
                    float(payload.get("commanded_mm", 0.0)),
                )
                if candidate == key:
                    hit = record
                    break
            if hit is None:
                audit_missing.append(
                    {"ts": decision["ts"], "epoch": decision["epoch"], "op_id": decision["op_id"]}
                )
            else:
                pool.remove(hit)

        migrations = [
            record
            for record in audit_records
            if record.action == "guide_baseline_migrated" and record.payload.get("unit") == self._unit
        ]
        migration_keys = {
            (int(record.payload.get("from_epoch", -1)), int(record.payload.get("to_epoch", -1)))
            for record in migrations
        }
        transitions = set()
        for previous, current in zip(decisions, decisions[1:]):
            if current["epoch"] != previous["epoch"]:
                transitions.add((previous["epoch"], current["epoch"]))
        missing_migrations = [
            {"from_epoch": old, "to_epoch": new}
            for old, new in sorted(transitions)
            if (old, new) not in migration_keys
        ]

        return {
            "unit": self._unit,
            "epoch": self._epoch,
            "decisions_checked": len(decisions),
            "audit_compensation_records": len(compensated),
            "migrations_checked": len(migrations),
            "mismatches": mismatches,
            "audit_missing": audit_missing,
            "missing_migrations": missing_migrations,
            "consistent": not mismatches and not audit_missing and not missing_migrations,
        }
