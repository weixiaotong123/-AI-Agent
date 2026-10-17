from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


CURRENT_FILE = Path(__file__).resolve()
BACKEND_DIR = CURRENT_FILE.parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.services.city_registry_service import CityCoverageTier  # noqa: E402
from app.services.city_resolver_service import resolve_city  # noqa: E402
from app.services.place_candidate_service import (  # noqa: E402
    PlaceCandidateCategory,
    collect_city_candidate_pool,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="检查动态城市 POI 候选覆盖。")
    parser.add_argument("city", help="待检查城市，例如上海")
    args = parser.parse_args()

    resolution = resolve_city(args.city)
    if resolution.tier is CityCoverageTier.INSUFFICIENT_DATA:
        print(
            json.dumps(
                {
                    "city": resolution.city,
                    "tier": resolution.tier.value,
                    "message": "地图服务无法确认该目的地。",
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1

    pool = collect_city_candidate_pool(
        city=resolution.city,
        adcode=resolution.adcode,
        administrative_level=resolution.administrative_level,
    )
    result = {
        "city": pool.city,
        "tier": resolution.tier.value,
        "adcode": resolution.adcode,
        "administrative_level": resolution.administrative_level,
        "meets_minimum": pool.meets_minimum,
        "counts": {
            category.value: len(pool.candidates_for(category))
            for category in PlaceCandidateCategory
        },
        "shortages": {
            category.value: shortage
            for category, shortage in pool.shortages.items()
        },
        "samples": {
            category.value: [
                candidate.name
                for candidate in pool.candidates_for(category)[:5]
            ]
            for category in PlaceCandidateCategory
        },
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if pool.meets_minimum else 2


if __name__ == "__main__":
    raise SystemExit(main())
