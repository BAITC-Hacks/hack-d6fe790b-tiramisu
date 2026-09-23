"""Verify the saved semantic snapshot and recommendations without API calls.

Run after prepare_embeddings.py:
    python scripts/verify_embeddings.py

This command never reads an API key or replaces the saved artifact. Exit 2
means the artifact is absent/invalid. Passing checks verifies integration, not
human judgments about which vendor should be preferred.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.recommendation_service import RecommendationService, load_catalog  # noqa: E402
from backend.ranking import DEFAULT_EMBEDDINGS_PATH  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact", type=Path, default=DEFAULT_EMBEDDINGS_PATH)
    args = parser.parse_args(argv)
    profiles, source = load_catalog(ROOT / "backend" / "data")
    semantic = RecommendationService(profiles, source, embeddings_path=args.artifact)
    if semantic.ranking.mode != "semantic":
        print(json.dumps({"verified": False, "ranking": semantic.ranking.metadata()}))
        return 2
    restarted = RecommendationService(profiles, source, embeddings_path=args.artifact)
    lexical = RecommendationService(profiles, source, embeddings_path=None)
    manifest = json.loads((ROOT / "backend" / "data" / "demo-scenarios.json").read_text(encoding="utf-8"))
    if manifest.get("catalog_version") != semantic.catalog_version:
        print("Scenario catalog version does not match; update the demo scenarios before verification.")
        return 2
    summary = []
    for scenario in manifest["scenarios"]:
        query = scenario["request"]
        started = time.perf_counter()
        result, status = semantic.recommend(query)
        elapsed = time.perf_counter() - started
        repeat, _ = semantic.recommend(query)
        fresh, _ = restarted.recommend(query)
        baseline, _ = lexical.recommend(query)
        ids = lambda response: [card["id"] for card in response["results"]]
        if status != 200 or elapsed >= 10 or ids(result) != ids(repeat) or ids(result) != ids(fresh):
            raise RuntimeError(f"Unstable or slow semantic result: {scenario['id']}")
        for field in ("outcome", "total_eligible", "rejection_counts", "availability_summary"):
            if result[field] != baseline[field]:
                raise RuntimeError(f"Hard filters differ between ranking modes: {scenario['id']}, {field}")
        if result["outcome"] != scenario["expected"]["outcome"] or result["total_eligible"] != scenario["expected"]["total_eligible"]:
            raise RuntimeError(f"Unexpected demo outcome: {scenario['id']}")
        summary.append({"scenario": scenario["id"], "semantic_ids": ids(result), "lexical_ids": ids(baseline), "ms": round(elapsed * 1000, 2)})
    print(json.dumps({"verified": True, "catalog_version": semantic.catalog_version, "ranking": semantic.ranking.metadata(), "scenarios": summary}, ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
