"""人生规划系统 (lifeplan)。

把抽象目标变成可核验指标，把自我判断变成可校准预测。

核心命题：万事万物皆可量化 —— 但不可核验的量化，只值一半分。
"""

__version__ = "0.1.0"

from .db import connect, init_db, db_path
from .metrics import build_snapshot, normalize_value
from .calibration import compute_calibration, load_predictions
from .evidence import tier_label, tier_weight

__all__ = [
    "connect",
    "init_db",
    "db_path",
    "build_snapshot",
    "normalize_value",
    "compute_calibration",
    "load_predictions",
    "tier_label",
    "tier_weight",
]
