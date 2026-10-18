"""Generate six explicitly synthetic reports and their reproducible manifest."""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FUNDS = [
    ("harbor_credit", "Harbor Credit Demo Fund", "Private credit and senior secured loans.",
     [(120000000, 200000000, 1000000), (126000000, 200000000, 1500000)]),
    ("summit_growth", "Summit Growth Demo Fund", "Venture growth equity in fictional companies.",
     [(80000000, 150000000, 0), (76000000, 160000000, 500000)]),
    ("cedar_real_assets", "Cedar Real Assets Demo Fund", "Real estate and infrastructure assets.",
     [(90000000, 140000000, 700000), (94500000, 140000000, 800000)]),
]


def generate() -> None:
    data = ROOT / "data"
    data.mkdir(exist_ok=True)
    files = []
    for fund_id, name, strategy, quarters in FUNDS:
        for period, values in zip(["2026Q1", "2026Q2"], quarters):
            doc_id = f"{fund_id}_{period.lower()}"
            nav, commitments, distributions = values
            text = (
                "SYNTHETIC REPORT - LOCAL PROTOTYPE ONLY\n"
                f"Document ID: {doc_id}\nFund ID: {fund_id}\nFund: {name}\n"
                f"Period: {period}\nCurrency: USD\nStrategy: {strategy}\n"
                f"NAV USD: {nav}\nCommitments USD: {commitments}\n"
                f"Distributions USD: {distributions}\n"
                "All names and amounts are invented demonstration data.\n"
                "Changes in NAV are not investment returns and may reflect cash flows.\n"
            )
            payload = text.encode("utf-8")
            filename = f"{doc_id}.txt"
            (data / filename).write_bytes(payload)
            files.append({"file": filename, "sha256": hashlib.sha256(payload).hexdigest()})
    manifest = {"dataset_version": "synthetic-reports-v1", "synthetic": True, "files": files}
    (data / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")


if __name__ == "__main__":
    generate()
