"""导焦栅漂移补偿的固定策略口径。

补偿粒度在代码里定死：**按炉组（battery group）独立计算**，不提供"全厂统一"
口径——各炉组机械磨损互不相干，一个全局偏移会把甲炉组的磨损摊到乙炉组上。
配置里写任何其他粒度都在装配期被 :class:`ConfigError` 拦下。

漂移超限后的处置同样在这里收口，取值只有两档：

``compensate``   漂移超限时继续自动补偿（每次仍报 warning），适合磨损稳定、
                 补偿量仍在物理允许范围内的炉组；
``recalibrate``  漂移超限即冻结补偿并禁止锁栅，停下来要求人工重新标定。
"""

from __future__ import annotations

# 补偿粒度定死按炉组；刻意不定义 plant/全厂 常量。
SCOPE_BATTERY_GROUP = "battery_group"
COMPENSATION_SCOPES: tuple[str, ...] = (SCOPE_BATTERY_GROUP,)

# 漂移超限处置策略。
POLICY_COMPENSATE = "compensate"
POLICY_RECALIBRATE = "recalibrate"
OVERLIMIT_POLICIES: tuple[str, ...] = (POLICY_COMPENSATE, POLICY_RECALIBRATE)

# 结论动作（同时是审计 action，对账时按这组名字过滤）。
ACTION_MONITOR = "guide_drift_monitor"
ACTION_HOLD = "guide_drift_hold"
ACTION_COMPENSATED = "guide_target_compensated"
ACTION_COMPENSATED_OVER_LIMIT = "guide_target_compensated_over_limit"
ACTION_RECALIBRATION_REQUIRED = "guide_recalibration_required"
ACTION_BASELINE_MIGRATED = "guide_baseline_migrated"

# 漂移相关审计动作全集，供历史复查对账使用。
DRIFT_ACTIONS: tuple[str, ...] = (
    ACTION_MONITOR,
    ACTION_HOLD,
    ACTION_COMPENSATED,
    ACTION_COMPENSATED_OVER_LIMIT,
    ACTION_RECALIBRATION_REQUIRED,
    ACTION_BASELINE_MIGRATED,
)

# 停机重新标定时，推焦编排返回的违约项。
VIOLATION_RECALIBRATION_REQUIRED = "guide_recalibration_required"
