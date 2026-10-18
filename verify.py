"""Deterministic local checks, including an actual MCP stdio subprocess.

Run: .venv/bin/python verify.py
This evaluates protocol and fixture-backed software behavior, not model accuracy,
retrieval relevance on real documents, production readiness, or financial returns.
"""
from __future__ import annotations

import asyncio
import hashlib
from importlib.metadata import version
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile

from mcp import Client, StdioServerParameters

from report_store import ReportStore

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
REPORT = ROOT / "evaluation" / "verification.json"
TOOLS = {"search_reports", "read_report", "get_metric", "compare_periods"}
EXPECTED = {
    "harbor_credit_2026q1": ("harbor_credit", "2026Q1", 120_000_000, 200_000_000, 1_000_000),
    "harbor_credit_2026q2": ("harbor_credit", "2026Q2", 126_000_000, 200_000_000, 1_500_000),
    "summit_growth_2026q1": ("summit_growth", "2026Q1", 80_000_000, 150_000_000, 0),
    "summit_growth_2026q2": ("summit_growth", "2026Q2", 76_000_000, 160_000_000, 500_000),
    "cedar_real_assets_2026q1": ("cedar_real_assets", "2026Q1", 90_000_000, 140_000_000, 700_000),
    "cedar_real_assets_2026q2": ("cedar_real_assets", "2026Q2", 94_500_000, 140_000_000, 800_000),
}
METRICS = ("nav_usd", "commitments_usd", "distributions_usd")
CHECKS: list[dict] = []
TOOL_CALLS = 0
MANIFEST_BYTES = (DATA / "manifest.json").read_bytes()
MANIFEST = json.loads(MANIFEST_BYTES)
DATA_HASH = hashlib.sha256(MANIFEST_BYTES).hexdigest()
HASHES = {Path(item["file"]).stem: item["sha256"] for item in MANIFEST["files"]}


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


async def check(name: str, category: str, operation) -> None:
    try:
        result = operation()
        if hasattr(result, "__await__"):
            await result
    except Exception as error:
        CHECKS.append({"name": name, "category": category, "status": "failed",
                       "detail": f"{type(error).__name__}: {error}"})
    else:
        CHECKS.append({"name": name, "category": category, "status": "passed"})


def envelope(payload: dict) -> None:
    require(payload.get("synthetic") is True, "Missing synthetic-data disclosure")
    require(payload.get("dataset_version") == MANIFEST["dataset_version"], "Wrong dataset version")
    require(payload.get("dataset_sha256") == DATA_HASH, "Wrong dataset manifest hash")


def source(value: dict, document_id: str) -> None:
    fund, period, *_ = EXPECTED[document_id]
    require(value.get("document_id") == document_id, "Incorrect document citation")
    require(value.get("fund_id") == fund and value.get("period") == period, "Wrong cited fund/period")
    require(value.get("uri") == f"synthetic://reports/{document_id}", "Unexpected source URI")
    digest = hashlib.sha256((DATA / f"{document_id}.txt").read_bytes()).hexdigest()
    require(value.get("sha256") == HASHES[document_id] == digest, "Source hash does not match bytes")


async def invoke(client, name: str, arguments: dict, *, error: str | None = None) -> dict | None:
    global TOOL_CALLS
    TOOL_CALLS += 1
    result = await client.call_tool(name, arguments)
    texts = [block.text for block in result.content if getattr(block, "type", None) == "text"]
    if error is not None:
        require(result.is_error is True, f"{name} accepted invalid input: {arguments!r}")
        require(error.lower() in "\n".join(texts).lower(), f"Unexpected error response: {texts!r}")
        return None
    require(not result.is_error, f"{name} unexpectedly returned an error: {texts!r}")
    payload = result.structured_content
    if payload is None:
        require(len(texts) == 1, "Expected one JSON result or structured content")
        payload = json.loads(texts[0])
    require(isinstance(payload, dict), "Tool did not return an object")
    envelope(payload)
    return payload


def verify_manifest() -> None:
    require(MANIFEST.get("synthetic") is True, "Manifest is not marked synthetic")
    require(set(HASHES) == set(EXPECTED), "Fixture collection does not match this test dataset")
    require(len(MANIFEST["files"]) == len(EXPECTED), "Duplicate fixture entries")
    for document_id in EXPECTED:
        raw = (DATA / f"{document_id}.txt").read_bytes()
        require(hashlib.sha256(raw).hexdigest() == HASHES[document_id], f"Corrupt fixture {document_id}")


def reject_fixture(kind: str, expected_error: str) -> None:
    with tempfile.TemporaryDirectory(prefix="investment-mcp-verify-") as temporary:
        copied = Path(temporary) / "data"
        shutil.copytree(DATA, copied)
        manifest = json.loads((copied / "manifest.json").read_text())
        first = manifest["files"][0]
        path = copied / first["file"]
        if kind == "tampered_bytes":
            path.write_bytes(path.read_bytes() + b"Undocumented modification\n")
        elif kind == "manifest_escape":
            first["file"] = "../outside.txt"
        elif kind == "nonsynthetic":
            manifest["synthetic"] = False
        elif kind == "missing_label":
            path.write_text(path.read_text().replace("SYNTHETIC REPORT - LOCAL PROTOTYPE ONLY\n", ""))
            first["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        elif kind == "unsupported_currency":
            path.write_text(path.read_text().replace("Currency: USD", "Currency: EUR"))
            first["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        elif kind == "negative_amount":
            path.write_text(path.read_text().replace("NAV USD: 120000000", "NAV USD: -1"))
            first["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        elif kind == "symlink_escape":
            outside = Path(temporary) / "outside.txt"
            outside.write_bytes(path.read_bytes())
            path.unlink()
            path.symlink_to(outside)
        else:
            raise AssertionError(f"Unknown fixture mutation: {kind}")
        (copied / "manifest.json").write_text(json.dumps(manifest))
        try:
            store = ReportStore(copied)
        except (ValueError, sqlite3.IntegrityError) as error:
            require(expected_error.lower() in str(error).lower(), f"Wrong rejection: {error}")
        else:
            store.connection.close()
            raise AssertionError(f"ReportStore accepted invalid fixture mutation: {kind}")


def verify_read_only() -> None:
    store = ReportStore(DATA)
    try:
        try:
            store.connection.execute("DELETE FROM reports")
        except sqlite3.OperationalError as error:
            require("readonly" in str(error).lower(), f"Wrong mutation failure: {error}")
        else:
            raise AssertionError("SQLite query-only guard allowed a write")
    finally:
        store.connection.close()


async def main() -> int:
    await check("manifest_and_all_source_hashes", "fixture_integrity", verify_manifest)
    for kind, message in (
        ("tampered_bytes", "hash mismatch"),
        ("manifest_escape", "Invalid fixture filename"),
        ("nonsynthetic", "synthetic"),
        ("missing_label", "synthetic document label"),
        ("unsupported_currency", "currency"),
        ("negative_amount", "Negative fixture amount"),
        ("symlink_escape", "fixture"),
    ):
        await check(f"reject_{kind}", "fixture_integrity",
                    lambda k=kind, m=message: reject_fixture(k, m))
    await check("sqlite_index_rejects_mutation", "fixture_integrity", verify_read_only)

    protocol = None
    server_info = None
    parameters = StdioServerParameters(command=sys.executable,
                                       args=[str(ROOT / "server.py")], cwd=ROOT)
    try:
        async with Client(parameters, read_timeout_seconds=10) as client:
            protocol = client.protocol_version
            server_info = client.server_info.model_dump() if client.server_info else None

            def handshake():
                require(bool(protocol), "No negotiated MCP protocol")
                require(server_info is not None, "No server identity")
                require(server_info["name"] == "Investment Document MCP", "Unexpected server name")
                require(server_info["version"] == "0.1.0", "Unexpected server version")
            await check("stdio_initialization_and_server_identity", "mcp_integration", handshake)

            async def schemas():
                listing = await client.list_tools()
                require({tool.name for tool in listing.tools} == TOOLS, "Incorrect discovered tool set")
                by_name = {tool.name: tool.model_dump(by_alias=True) for tool in listing.tools}
                for name, tool in by_name.items():
                    schema = tool["inputSchema"]
                    require(schema["type"] == "object" and bool(schema["required"]), f"Missing input contract: {name}")
                    hints = tool["annotations"]
                    require(hints["readOnlyHint"] is True and hints["destructiveHint"] is False,
                            f"Incorrect tool safety annotations: {name}")
                limit = by_name["search_reports"]["inputSchema"]["properties"]["limit"]
                require(limit["minimum"] == 1 and limit["maximum"] == 10, "Missing search result bounds")
                allowed = by_name["get_metric"]["inputSchema"]["properties"]["metric"]["enum"]
                require(set(allowed) == set(METRICS), "Unbounded or incorrect metric enum")
                require("pattern" in by_name["read_report"]["inputSchema"]["properties"]["document_id"],
                        "Missing document identifier validation")
            await check("tool_discovery_input_schemas_and_read_only_hints", "mcp_integration", schemas)

            async def search_case(query, expected_ids, limit=5):
                result = await invoke(client, "search_reports", {"query": query, "limit": limit})
                ids = [item["source"]["document_id"] for item in result["results"]]
                require(ids == expected_ids, f"Unexpected search IDs for {query!r}: {ids}")
                for item in result["results"]:
                    source(item["source"], item["source"]["document_id"])
                    require(item["excerpt"] and item["title"], "Missing search preview/title")
            for name, query, expected_ids, limit in (
                ("search_known_strategy", "private credit", ["harbor_credit_2026q1", "harbor_credit_2026q2"], 5),
                ("search_all_terms_and_case", "HARBOR 2026Q2", ["harbor_credit_2026q2"], 5),
                ("search_limit_is_enforced", "synthetic", ["cedar_real_assets_2026q1", "cedar_real_assets_2026q2"], 2),
                ("search_no_matches", "unfindableword", [], 5),
                ("search_sql_like_injection_is_data", "' OR 1=1 -- absentneedle", [], 5),
            ):
                await check(name, "mcp_integration", lambda q=query, e=expected_ids, l=limit: search_case(q, e, l))

            async def read_case(document_id):
                result = await invoke(client, "read_report", {"document_id": document_id})
                require(result["text"] == (DATA / f"{document_id}.txt").read_text(), "Retrieved text differs from source bytes")
                source(result["source"], document_id)
            for document_id in EXPECTED:
                await check(f"read_and_cite_{document_id}", "mcp_integration", lambda d=document_id: read_case(d))

            async def metric_case(document_id):
                fund, period, *values = EXPECTED[document_id]
                for metric, expected in zip(METRICS, values):
                    result = await invoke(client, "get_metric", {"fund_id": fund, "period": period, "metric": metric})
                    require(result["value"] == expected and type(result["value"]) is int,
                            f"Wrong {document_id}/{metric}: expected {expected}, got {result['value']}")
                    require(result["unit"] == "USD" and result["metric"] == metric, "Wrong amount unit or metric")
                    source(result["source"], document_id)
            for document_id in EXPECTED:
                await check(f"all_three_metrics_{document_id}", "mcp_integration", lambda d=document_id: metric_case(d))

            async def comparison(fund, metric, start, end, delta, percent):
                args = {"fund_id": fund, "start_period": "2026Q1", "end_period": "2026Q2"}
                if metric != "nav_usd":
                    args["metric"] = metric
                result = await invoke(client, "compare_periods", args)
                require((result["start_value"], result["end_value"], result["delta"], result["percent_change"])
                        == (start, end, delta, percent), f"Incorrect {fund}/{metric} comparison: {result}")
                require(result["unit"] == "USD" and result["metric"] == metric, "Wrong comparison units/metric")
                require(len(result["sources"]) == 2, "Missing before/after provenance")
                source(result["sources"][0], f"{fund}_2026q1")
                source(result["sources"][1], f"{fund}_2026q2")
                require("not a total investment return" in result["interpretation"], "Missing financial interpretation limitation")
                if percent is None:
                    require(result["percent_change_note"] == "undefined for zero baseline", "Zero baseline must be explicit")
            for name, arguments in (
                ("compare_positive_default_nav", ("harbor_credit", "nav_usd", 120_000_000, 126_000_000, 6_000_000, "5.00")),
                ("compare_negative_nav", ("summit_growth", "nav_usd", 80_000_000, 76_000_000, -4_000_000, "-5.00")),
                ("compare_unchanged_commitments", ("harbor_credit", "commitments_usd", 200_000_000, 200_000_000, 0, "0.00")),
                ("compare_zero_baseline", ("summit_growth", "distributions_usd", 0, 500_000, 500_000, None)),
                ("compare_fractional_percentage", ("cedar_real_assets", "distributions_usd", 700_000, 800_000, 100_000, "14.29")),
            ):
                await check(name, "mcp_integration", lambda a=arguments: comparison(*a))

            failures = [
                ("unknown_document", "read_report", {"document_id": "unknown_report"}, "Unknown document ID"),
                ("path_escape_document_id", "read_report", {"document_id": "../server.py"}, "pattern"),
                ("missing_required_argument", "read_report", {}, "required"),
                ("wrong_identifier_type", "read_report", {"document_id": 123}, "string"),
                ("unknown_fund", "get_metric", {"fund_id": "unknown", "period": "2026Q1", "metric": "nav_usd"}, "No report"),
                ("missing_period_data", "get_metric", {"fund_id": "harbor_credit", "period": "2025Q4", "metric": "nav_usd"}, "No report"),
                ("unsupported_metric", "get_metric", {"fund_id": "harbor_credit", "period": "2026Q1", "metric": "returns"}, "Input should be"),
                ("invalid_period_format", "get_metric", {"fund_id": "harbor_credit", "period": "2026Q5", "metric": "nav_usd"}, "pattern"),
                ("reversed_periods", "compare_periods", {"fund_id": "harbor_credit", "start_period": "2026Q2", "end_period": "2026Q1"}, "must precede"),
                ("equal_periods", "compare_periods", {"fund_id": "harbor_credit", "start_period": "2026Q1", "end_period": "2026Q1"}, "must precede"),
                ("compare_missing_endpoint", "compare_periods", {"fund_id": "harbor_credit", "start_period": "2026Q1", "end_period": "2026Q3"}, "No report"),
                ("empty_query", "search_reports", {"query": ""}, "at least 1"),
                ("punctuation_only_query", "search_reports", {"query": "!!!"}, "searchable word"),
                ("excessive_query_length", "search_reports", {"query": "x" * 161}, "at most 160"),
                ("limit_above_maximum", "search_reports", {"query": "credit", "limit": 11}, "less than or equal"),
                ("limit_below_minimum", "search_reports", {"query": "credit", "limit": 0}, "greater than or equal"),
                ("limit_string_not_coerced", "search_reports", {"query": "credit", "limit": "2"}, "valid integer"),
            ]
            for name, tool, args, error in failures:
                await check(f"reject_{name}", "mcp_integration",
                            lambda t=tool, a=args, e=error: invoke(client, t, a, error=e))
    except Exception as error:
        CHECKS.append({"name": "mcp_session", "category": "mcp_integration", "status": "failed",
                       "detail": f"{type(error).__name__}: {error}"})

    failed = sum(item["status"] == "failed" for item in CHECKS)
    report = {
        "verification_version": "1.0.0",
        "scope": "Local synthetic-data prototype; software/protocol verification only, not model accuracy or production validation.",
        "mcp_sdk_version": version("mcp"),
        "protocol_version": protocol,
        "server_info": server_info,
        "dataset_version": MANIFEST["dataset_version"],
        "dataset_sha256": DATA_HASH,
        "fixture_documents": len(EXPECTED),
        "fixture_metric_values_checked": len(EXPECTED) * len(METRICS),
        "tool_calls": TOOL_CALLS,
        "source_sha256": {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
                          for name in ("server.py", "report_store.py", "verify.py")},
        "counts": {"total": len(CHECKS), "passed": len(CHECKS) - failed, "failed": failed},
        "checks": CHECKS,
    }
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"report": str(REPORT), **report["counts"], "tool_calls": TOOL_CALLS}))
    for item in CHECKS:
        if item["status"] == "failed":
            print(f"FAIL {item['name']}: {item['detail']}")
    return int(failed > 0)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
