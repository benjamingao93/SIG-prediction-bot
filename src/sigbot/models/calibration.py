"""Reliability table: when the price says p, how often does YES actually happen?"""
from __future__ import annotations

from typing import Iterable, List, Tuple


def reliability_table(pairs: Iterable[Tuple[float, int]], bins: int = 10) -> List[dict]:
    buckets: List[List[Tuple[float, int]]] = [[] for _ in range(bins)]
    for p, y in pairs:
        buckets[min(bins - 1, int(p * bins))].append((p, y))
    out = []
    for i, b in enumerate(buckets):
        if not b:
            continue
        out.append({
            "lo": i / bins,
            "hi": (i + 1) / bins,
            "n": len(b),
            "mean_p": sum(p for p, _ in b) / len(b),
            "freq_yes": sum(y for _, y in b) / len(b),
        })
    return out


def brier(pairs: Iterable[Tuple[float, int]]) -> float:
    pairs = list(pairs)
    return sum((p - y) ** 2 for p, y in pairs) / len(pairs) if pairs else float("nan")
