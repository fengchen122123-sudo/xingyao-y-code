"""Run the three real computer probes repeatedly and record every result."""
from __future__ import annotations

import argparse
import importlib.util
import json
import statistics
import time
from pathlib import Path


def _load_audit():
    path = Path(__file__).with_name("audit_computer_live.py")
    spec = importlib.util.spec_from_file_location("ycode_audit_live", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _summarize(records):
    durations = [r["duration_ms"] for r in records]
    successes = sum(r["ok"] for r in records)
    return {
        "samples": len(records),
        "success": successes,
        "success_rate": successes / len(records) if records else 0.0,
        "p50_ms": statistics.median(durations) if durations else None,
        "p95_ms": sorted(durations)[max(0, int(len(durations) * 0.95) - 1)] if durations else None,
        "min_ms": min(durations) if durations else None,
        "max_ms": max(durations) if durations else None,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--rounds", type=int, default=10)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--browser", choices=["edge", "chrome"], default="edge")
    args = parser.parse_args()
    if args.rounds < 1:
        parser.error("--rounds must be positive")
    audit = _load_audit()
    probes = {
        "desktop_input": audit.desktop_input_probe,
        "desktop_surfaces": audit.desktop_surfaces_probe,
        "browser_process": lambda: audit.browser_process_probe(args.browser),
    }
    output = {"date": "2026-09-05", "rounds": args.rounds, "browser": args.browser, "probes": {}}
    for name, probe in probes.items():
        records = []
        for index in range(1, args.rounds + 1):
            started = time.perf_counter()
            try:
                cases = probe()
                ok = all(bool(item.get("ok")) for item in cases)
                failures = [{"case": item.get("case"), "error_code": item.get("error_code", "")} for item in cases if not item.get("ok")]
            except Exception as exc:
                ok = False
                cases = []
                failures = [{"case": "probe", "error_code": getattr(exc, "code", ""), "error": str(exc)}]
            record = {"round": index, "ok": ok, "duration_ms": round((time.perf_counter() - started) * 1000, 1), "failures": failures, "cases": cases}
            records.append(record)
            print(json.dumps({"probe": name, "round": index, "ok": ok, "duration_ms": record["duration_ms"], "failures": failures}, ensure_ascii=False), flush=True)
        output["probes"][name] = {"runs": records, "summary": _summarize(records)}
        print(name, output["probes"][name]["summary"], flush=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0 if all(p["summary"]["success"] == args.rounds for p in output["probes"].values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
