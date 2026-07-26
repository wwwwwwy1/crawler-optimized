"""按 AB/BA 顺序重复运行 Web Demo，降低网络和执行顺序偏差。"""

import argparse
import json
import statistics
import subprocess
import sys
import time
from pathlib import Path


def nested(metrics, path):
    value = metrics
    for key in path.split("."):
        value = value.get(key, 0)
    return value


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--input", default="input/bilibili_validation_urls.csv"
    )
    parser.add_argument("--output-root", default="")
    parser.add_argument("--pairs", type=int, default=2)
    parser.add_argument("--max-pages", type=int, default=5)
    parser.add_argument("--max-runtime", type=int, default=180)
    parser.add_argument("--timeout-ms", type=int, default=20000)
    parser.add_argument("--discover", action="store_true")
    args = parser.parse_args()

    root = Path(args.output_root or f"output/ab_{int(time.time())}")
    root.mkdir(parents=True, exist_ok=True)
    results = {"baseline": [], "optimized": []}

    for pair in range(args.pairs):
        order = (
            ("baseline", "optimized")
            if pair % 2 == 0
            else ("optimized", "baseline")
        )
        for mode in order:
            run_dir = root / f"pair_{pair + 1}_{mode}"
            command = [
                sys.executable,
                "optimized_demo.py",
                "--mode", mode,
                "--input", args.input,
                "--output", str(run_dir),
                "--max-pages", str(args.max_pages),
                "--max-runtime", str(args.max_runtime),
                "--timeout-ms", str(args.timeout_ms),
                "--request-interval", "0.5",
            ]
            if mode == "optimized":
                command.extend(["--max-depth", "3"])
            if args.discover:
                command.append("--discover")
            print(f"[RUN] pair={pair + 1} mode={mode}")
            completed = subprocess.run(command, check=False)
            if completed.returncode != 0:
                raise SystemExit(
                    f"{mode} failed with code {completed.returncode}"
                )
            with open(run_dir / "metrics.json", "r", encoding="utf-8") as source:
                results[mode].append(json.load(source))

    fields = [
        "elapsed_seconds",
        "rates.pages_per_minute",
        "rates.screenshots_per_hour",
        "rates.saved_per_loaded_page",
        "timings.page_load.average_seconds",
        "timings.state_wait.average_seconds",
        "timings.screenshot.average_seconds",
        "unique_states",
        "unique_url_families",
        "max_depth_visited",
        "counters.links_enqueued",
        "counters.screenshots_saved",
        "counters.load_failures",
    ]
    aggregate = {}
    for mode, runs in results.items():
        aggregate[mode] = {
            field: statistics.median(nested(run, field) for run in runs)
            for field in fields
        }

    comparison = {}
    for field in fields:
        before = aggregate["baseline"][field]
        after = aggregate["optimized"][field]
        comparison[field] = {
            "baseline_median": before,
            "optimized_median": after,
            "change_ratio": (after - before) / before if before else None,
        }

    summary = {
        "pairs": args.pairs,
        "max_pages": args.max_pages,
        "discover": args.discover,
        "runs": results,
        "median": aggregate,
        "comparison": comparison,
    }
    with open(root / "ab_summary.json", "w", encoding="utf-8") as output:
        json.dump(summary, output, ensure_ascii=False, indent=2)

    lines = [
        "# Web 算法 AB/BA 对比",
        "",
        f"- 每种模式运行次数：{args.pairs}",
        f"- 每次页面上限：{args.max_pages}",
        "",
        "| 指标 | Baseline 中位数 | Optimized 中位数 | 变化 |",
        "|---|---:|---:|---:|",
    ]
    for field in fields:
        item = comparison[field]
        ratio = item["change_ratio"]
        change = "N/A" if ratio is None else f"{ratio * 100:+.1f}%"
        lines.append(
            f"| `{field}` | {item['baseline_median']:.3f} | "
            f"{item['optimized_median']:.3f} | {change} |"
        )
    (root / "ab_summary.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"[DONE] {root.resolve()}")


if __name__ == "__main__":
    main()
