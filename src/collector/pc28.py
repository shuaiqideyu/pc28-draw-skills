"""Keno → PC28 与衍生字段纯函数（对齐 packages/shared/src/market/pc28.ts）。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

GROUP1 = [2, 5, 8, 11, 14, 17]
GROUP2 = [3, 6, 9, 12, 15, 18]
GROUP3 = [4, 7, 10, 13, 16, 19]


def is_valid_keno_nums(nums: object) -> bool:
    if not isinstance(nums, list) or len(nums) != 20:
        return False
    return all(
        isinstance(n, int) and not isinstance(n, bool) and 1 <= n <= 80 for n in nums
    )


def transform_keno_to_pc28(nums: list[int]) -> tuple[int, int, int, int]:
    if not is_valid_keno_nums(nums):
        raise ValueError("无效 Keno 号码：需恰好 20 个且均在 1..80")
    sorted_nums = sorted(nums)
    sum_group = lambda pos: sum(sorted_nums[p - 1] for p in pos) % 10  # noqa: E731
    num1 = sum_group(GROUP1)
    num2 = sum_group(GROUP2)
    num3 = sum_group(GROUP3)
    return num1, num2, num3, num1 + num2 + num3


def calc_size(draw_sum: int) -> Literal["大", "小"]:
    return "大" if draw_sum >= 14 else "小"


def calc_parity(draw_sum: int) -> Literal["单", "双"]:
    return "单" if draw_sum % 2 != 0 else "双"


def calc_extreme(draw_sum: int) -> Literal["极大", "极小"] | None:
    if draw_sum >= 22:
        return "极大"
    if draw_sum <= 5:
        return "极小"
    return None


def calc_combination(draw_sum: int) -> str:
    return calc_size(draw_sum) + calc_parity(draw_sum)


def calc_pattern(num1: int, num2: int, num3: int) -> str:
    if num1 == num2 == num3:
        return "豹子"
    if num1 == num2 or num2 == num3 or num1 == num3:
        return "对子"
    sorted_nums = sorted([num1, num2, num3])
    d1 = sorted_nums[1] - sorted_nums[0]
    d2 = sorted_nums[2] - sorted_nums[1]
    # 先排序，不看先后。传统模式只认线性三连。
    # 890（0、8、9）和 901（0、1、9）只在高倍模式下算顺子，任意顺序都一样。
    if d1 == 1 and d2 == 1:
        return "顺子"
    return "杂六"


def calc_location(draw_sum: int) -> Literal["小边", "中", "大边"]:
    if draw_sum <= 9:
        return "小边"
    if draw_sum <= 17:
        return "中"
    return "大边"


def calc_dragon_tiger(num1: int, num3: int) -> Literal["龙", "虎", "合"]:
    if num1 > num3:
        return "龙"
    if num1 < num3:
        return "虎"
    return "合"


@dataclass(frozen=True)
class DerivedFields:
    size_type: Literal["大", "小"]
    parity_type: Literal["单", "双"]
    extreme_type: Literal["极大", "极小"] | None
    combination_type: str
    pattern_type: str
    location_type: Literal["小边", "中", "大边"]
    dragon_tiger: Literal["龙", "虎", "合"]


def calculate_derived(
    num1: int, num2: int, num3: int, draw_sum: int
) -> DerivedFields:
    return DerivedFields(
        size_type=calc_size(draw_sum),
        parity_type=calc_parity(draw_sum),
        extreme_type=calc_extreme(draw_sum),
        combination_type=calc_combination(draw_sum),
        pattern_type=calc_pattern(num1, num2, num3),
        location_type=calc_location(draw_sum),
        dragon_tiger=calc_dragon_tiger(num1, num3),
    )


def build_draw_number(num1: int, num2: int, num3: int) -> str:
    return f"{num1}+{num2}+{num3}"
