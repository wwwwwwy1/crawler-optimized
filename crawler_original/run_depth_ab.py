"""在同一模拟器上按 AB/BA 顺序比较 MAX_DEPTH=1 与更深遍历。"""

import argparse
import json
import signal
import statistics
import subprocess
import time
from pathlib import Path

from core.adb_bin import ADB


PACKAGE_NAME = "tv.danmaku.bili"
METRIC_NAMES = (
    "elapsed_seconds",
    "screenshots",
    "screenshots_per_hour",
    "near_unique_images_per_hour",
    "near_duplicate_ratio",
    "actions",
    "actions_per_screenshot",
    "max_depth_invoked",
    "unique_activities",
    "exact_unique_images",
    "near_unique_images_dhash12",
    "deep_actions_pruned",
    "deep_activity_expansion_skips",
    "backtrack_failures",
    "backtrack_seconds",
    "restart_calls",
    "restart_seconds",
)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--deeper-depth", type=int, default=3)
    parser.add_argument("--pairs", type=int, default=2)
    parser.add_argument("--seconds", type=int, default=600)
    parser.add_argument("--emulator-host", default="127.0.0.1")
    parser.add_argument("--emulator-port", type=int, default=5555)
    parser.add_argument("--depth-1-action-limit", type=int, default=4)
    parser.add_argument("--depth-2-action-limit", type=int, default=2)
    parser.add_argument(
        "--max-deep-activity-expansions", type=int, default=2
    )
    parser.add_argument(
        "--python",
        default="/Users/bytedance/Downloads/项目/.venv-crawler/bin/python",
    )
    parser.add_argument("--output", default="")
    return parser.parse_args()


def emulator_preflight(host, port):
    serial = f"{host}:{port}"
    result = subprocess.run(
        [ADB, "connect", serial],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    print(f"[ADB] {result.stdout.strip() or result.stderr.strip()}")

    devices = subprocess.run(
        [ADB, "devices"],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    connected = {
        line.split("\t")[0]
        for line in devices.stdout.splitlines()[1:]
        if "\tdevice" in line
    }
    if serial not in connected:
        raise SystemExit(
            f"模拟器未连接：{serial}；当前设备：{sorted(connected)}"
        )

    package = subprocess.run(
        [ADB, "-s", serial, "shell", "pm", "path", PACKAGE_NAME],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if "package:" not in package.stdout:
        raise SystemExit(f"模拟器未安装 {PACKAGE_NAME}")
    return serial


def run_one(root, result_dir, variant, depth, args):
    result_dir.mkdir(parents=True, exist_ok=False)
    command = [
        args.python,
        "run_depth_variant.py",
        "--variant",
        variant,
        "--max-depth",
        str(depth),
        "--connection-mode",
        "emulator",
        "--emulator-host",
        args.emulator_host,
        "--emulator-port",
        str(args.emulator_port),
        "--depth-1-action-limit",
        str(args.depth_1_action_limit),
        "--depth-2-action-limit",
        str(args.depth_2_action_limit),
        "--max-deep-activity-expansions",
        str(args.max_deep_activity_expansions),
        "--output-dir",
        str(result_dir),
    ]

    process = subprocess.Popen(
        command,
        cwd=root,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    timed_out = False
    try:
        output, _ = process.communicate(timeout=args.seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        process.send_signal(signal.SIGINT)
        try:
            output, _ = process.communicate(timeout=45)
        except subprocess.TimeoutExpired:
            process.terminate()
            output, _ = process.communicate(timeout=15)

    (result_dir / "console.log").write_text(output, encoding="utf-8")
    metrics_path = result_dir / "experiment_metrics.json"
    if not metrics_path.exists():
        raise RuntimeError(
            f"{variant} 没有生成指标，查看 {result_dir / 'console.log'}"
        )
    metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
    metrics.update(
        {
            "returncode": process.returncode,
            "timed_out": timed_out,
            "result_dir": str(result_dir),
        }
    )
    return metrics


def percentage_change(baseline, deeper):
    if baseline == 0:
        return "N/A"
    return f"{(deeper - baseline) / baseline:+.1%}"


def format_metric(name, value):
    if name == "near_duplicate_ratio":
        return f"{value:.1%}"
    return f"{value:.3f}"


def aggregate(runs):
    result = {}
    for variant in ("baseline", "deeper"):
        selected = [run for run in runs if run["variant"] == variant]
        result[variant] = {
            name: statistics.median(float(run.get(name, 0)) for run in selected)
            for name in METRIC_NAMES
        }
    return result


def write_markdown(path, summary, args):
    baseline = summary["aggregate"]["baseline"]
    deeper = summary["aggregate"]["deeper"]
    labels = {
        "elapsed_seconds": "总耗时（秒）",
        "screenshots": "截图数",
        "screenshots_per_hour": "截图/小时",
        "near_unique_images_per_hour": "dHash 去近重后图片/小时",
        "near_duplicate_ratio": "dHash 近重复占比",
        "actions": "点击动作数",
        "actions_per_screenshot": "动作/截图",
        "max_depth_invoked": "实际调用最大层级",
        "unique_activities": "唯一 Activity",
        "exact_unique_images": "SHA-256 唯一图片",
        "near_unique_images_dhash12": "dHash 去近重后图片",
        "deep_actions_pruned": "深层裁剪动作数",
        "deep_activity_expansion_skips": "同 Activity 停止展开次数",
        "backtrack_failures": "深层回退失败数",
        "backtrack_seconds": "深层回退耗时（秒）",
        "restart_calls": "App 重启次数",
        "restart_seconds": "App 重启耗时（秒）",
    }
    lines = [
        "# MAX_DEPTH 模拟器 AB/BA 实验",
        "",
        "- 对照组：`MAX_DEPTH=1`",
        f"- 实验组：`MAX_DEPTH={args.deeper_depth}`",
        f"- Depth 1 动作上限：{args.depth_1_action_limit}",
        f"- Depth 2 动作上限：{args.depth_2_action_limit}",
        "- 同一深层 Activity 最多展开："
        f"{args.max_deep_activity_expansions} 次",
        f"- 每组运行次数：{args.pairs}",
        f"- 每次最长运行：{args.seconds} 秒",
        "",
        "| 指标 | Depth=1 中位数 | "
        f"Depth={args.deeper_depth} 中位数 | 变化 |",
        "|---|---:|---:|---:|",
    ]
    for name in METRIC_NAMES:
        first = baseline[name]
        second = deeper[name]
        lines.append(
            f"| {labels[name]} | {format_metric(name, first)} | "
            f"{format_metric(name, second)} | "
            f"{percentage_change(first, second)} |"
        )
    lines.extend(
        [
            "",
            "判断时优先看 `dHash 去近重后图片/小时`，不能只看原始截图数。",
            "如果深度增加但去近重后的有效图片没有增加，说明只是采到了更多重复页。",
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")


def main():
    args = parse_args()
    if args.deeper_depth <= 1:
        raise SystemExit("--deeper-depth 必须大于 1")
    if args.pairs < 1:
        raise SystemExit("--pairs 必须大于等于 1")
    if args.depth_1_action_limit < 1 or args.depth_2_action_limit < 1:
        raise SystemExit("深层动作上限必须大于等于 1")
    if args.max_deep_activity_expansions < 1:
        raise SystemExit("同 Activity 深层展开上限必须大于等于 1")

    root = Path(__file__).resolve().parent
    serial = emulator_preflight(args.emulator_host, args.emulator_port)
    result_root = Path(
        args.output
        or root / "depth_experiment_results" / str(int(time.time()))
    ).resolve()
    result_root.mkdir(parents=True, exist_ok=False)

    runs = []
    for pair in range(1, args.pairs + 1):
        order = (
            (("baseline", 1), ("deeper", args.deeper_depth))
            if pair % 2
            else (("deeper", args.deeper_depth), ("baseline", 1))
        )
        for variant, depth in order:
            print(f"[RUN] pair={pair} variant={variant} depth={depth}")
            run = run_one(
                root,
                result_root / f"pair_{pair}_{variant}_d{depth}",
                variant,
                depth,
                args,
            )
            run["pair"] = pair
            runs.append(run)

    summary = {
        "device": serial,
        "package": PACKAGE_NAME,
        "baseline_depth": 1,
        "deeper_depth": args.deeper_depth,
        "depth_action_limits": {
            "1": args.depth_1_action_limit,
            "2": args.depth_2_action_limit,
        },
        "max_deep_activity_expansions": (
            args.max_deep_activity_expansions
        ),
        "pairs": args.pairs,
        "seconds_per_run": args.seconds,
        "runs": runs,
        "aggregate": aggregate(runs),
    }
    (result_root / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    write_markdown(result_root / "summary.md", summary, args)
    print(f"[DONE] {result_root}")


if __name__ == "__main__":
    main()
