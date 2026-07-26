"""将 baseline 和 optimized 的统一指标生成对比报告。"""

import argparse
import json
from pathlib import Path


def load_metrics(path):
    with open(path, "r", encoding="utf-8") as source:
        return json.load(source)


def value(metrics, path, default=0):
    current = metrics
    for key in path.split("."):
        if not isinstance(current, dict):
            return default
        current = current.get(key, default)
    return current


def percent_change(before, after):
    if not before:
        return "N/A"
    return f"{(after - before) / before * 100:+.1f}%"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--optimized", required=True)
    parser.add_argument("--output", default="output/comparison.md")
    args = parser.parse_args()

    baseline = load_metrics(args.baseline)
    optimized = load_metrics(args.optimized)
    rows = [
        ("运行时间（秒）", "elapsed_seconds", False),
        ("成功加载页面", "counters.pages_loaded", True),
        ("唯一 UI 状态", "unique_states", True),
        ("URL 家族覆盖", "unique_url_families", True),
        ("保存截图", "counters.screenshots_saved", True),
        ("每分钟页面数", "rates.pages_per_minute", True),
        ("每小时截图数", "rates.screenshots_per_hour", True),
        ("每加载页有效截图", "rates.saved_per_loaded_page", True),
        ("加载失败", "counters.load_failures", False),
        ("近重复拒绝", "counters.near_duplicate", False),
    ]

    lines = [
        "# 算法效率对比",
        "",
        "| 指标 | Baseline | Optimized | 变化 | 越高越好 |",
        "|---|---:|---:|---:|:---:|",
    ]
    for label, path, higher_is_better in rows:
        before = value(baseline, path)
        after = value(optimized, path)
        lines.append(
            f"| {label} | {before:.3f} | {after:.3f} | "
            f"{percent_change(before, after)} | "
            f"{'是' if higher_is_better else '否'} |"
        )

    lines.extend([
        "",
        "说明：两次运行必须使用相同 URL、视口、页面上限、网络和机器。",
        "运行时间、失败数和重复数通常越低越好；覆盖度和有效产出通常越高越好。",
        "",
    ])

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(lines), encoding="utf-8")
    print(output.resolve())


if __name__ == "__main__":
    main()
