"""拦焦车。

拦焦车停在焦侧，负责把导焦栅对准炭化室并锁定，焦炭才能沿着导焦栅落到熄焦
车里。对齐与锁定是推焦的硬前置：不先对位、不先锁栅，推焦一律拒绝。对位目
标随机械磨损漂移，由补偿器按炉组自动修正，超限则按炉组策略报警或停机待标
定。
"""

from __future__ import annotations

from .align import Alignment
from .compensation import GuideCompensator, migrate_state
from .machine import GuideCar
from .snapshot import GuideSnapshot

__all__ = ["GuideCar", "GuideSnapshot", "Alignment", "GuideCompensator", "migrate_state"]
