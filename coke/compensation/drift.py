"""漂移趋势统计。

补偿的依据不是"最近一次对位歪了多少"，而是该炉组一个滑动窗口内**跟随误差**
（实测停留位置 − 当炉下发的有效目标）的均值（持续性漂移）与最小二乘斜率
（漂移趋势）。磨损是缓慢单调的过程，单次对位可能被焦炭卡滞、测量抖动污染，
必须先按窗口汇总，补偿器才能据此决策。

统计是纯函数，输入只依赖跟随误差序列，方便对同一份历史反复重算、交叉核对
当时的补偿结论是否站得住。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DriftStats:
    """一个偏差窗口的汇总。"""

    samples: int
    mean_mm: float
    slope_mm_per_sample: float
    latest_mm: float
    abs_mean_mm: float

    def as_dict(self) -> dict:
        return {
            "samples": self.samples,
            "mean_mm": round(self.mean_mm, 3),
            "slope_mm_per_sample": round(self.slope_mm_per_sample, 4),
            "latest_mm": round(self.latest_mm, 3),
            "abs_mean_mm": round(self.abs_mean_mm, 3),
        }


def drift_stats(deviations: list[float]) -> DriftStats:
    """对窗口内偏差（实测−标定名义目标，毫米）做汇总。

    斜率用最小二乘，自变量取 0..n-1 的对位序号；样本不足两条时斜率为 0。
    """

    count = len(deviations)
    if count == 0:
        return DriftStats(0, 0.0, 0.0, 0.0, 0.0)
    mean = sum(deviations) / count
    if count == 1:
        return DriftStats(1, mean, 0.0, deviations[-1], abs(mean))

    xs = range(count)
    x_mean = (count - 1) / 2.0
    numerator = sum((x - x_mean) * (y - mean) for x, y in zip(xs, deviations))
    denominator = sum((x - x_mean) ** 2 for x in xs)
    slope = numerator / denominator if denominator else 0.0
    return DriftStats(count, mean, slope, deviations[-1], abs(mean))
