"""Write the published JSON Schemas for every external interface to contracts/.
    python -m claimtriage.tools.export_contracts"""
import json
from pathlib import Path

from ..common.contracts import FNOL, FeedbackIn, Recommendation

OUT = Path(__file__).resolve().parents[2] / "contracts"


def main() -> None:
    OUT.mkdir(exist_ok=True)
    for name, model in (("fnol.v1.schema.json", FNOL), ("recommendation.v1.schema.json", Recommendation),
                        ("feedback.v1.schema.json", FeedbackIn)):
        (OUT / name).write_text(json.dumps(model.model_json_schema(), indent=2), encoding="utf-8")
        print("wrote", OUT / name)


if __name__ == "__main__":
    main()
