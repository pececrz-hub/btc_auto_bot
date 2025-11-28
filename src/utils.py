\
from decimal import Decimal

def to_decimal(x) -> Decimal:
    return Decimal(str(x))

def bps(x: float) -> float:
    return x * 10_000.0
