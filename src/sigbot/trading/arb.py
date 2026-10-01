"""Cross-market consistency violations the engine itself detects (e.g. linked races whose
prices contradict each other). Each comes with suggested corrective trades. v0: report only."""
from __future__ import annotations

from typing import Any, Dict, List

from ..api import markets as mk
from ..api.client import SigClient


def scan(client: SigClient, tournament_id: str) -> List[Dict[str, Any]]:
    return mk.relationship_violations(client, tournament_id)
