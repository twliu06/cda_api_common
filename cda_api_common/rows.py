from decimal import Decimal
import math


def clean_value(v):
    """處理 Decimal 與 NaN，讓 psycopg2 / pandas 的值可以直接序列化成 JSON。"""
    if isinstance(v, float) and math.isnan(v):
        return None
    if isinstance(v, Decimal):
        return None if v.is_nan() else float(v)
    return v


def clean_rows(rows: list[dict]) -> list[dict]:
    return [
        {k: clean_value(v) for k, v in row.items()}
        for row in rows
    ]
