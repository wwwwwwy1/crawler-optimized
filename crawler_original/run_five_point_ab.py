"""在同一模拟器上按 AB/BA 比较原行为与五项优化，固定 MAX_DEPTH=1。"""

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
    "unique_activities",
    "exact_unique_images",
    "near_unique_images_dhash12",
    "poco_attr_reads",
    "poco_attr_read_seconds",
    "hierarchy_dumps",
    "hierarchy_dump_seconds",
    "layout_duplicates_skipped",
    "image_duplicates_rejected",
    "capture_attempts",
    "capture_failures",
    "capture_retry_attempts",
    "capture_retry_recoveries",
    "click_failures",
    "parent_validation_failures",
    "parent_restore_failures",
    "fixed_wait_seconds",
    "adaptive_wait_seconds",
    "state_wait_timeouts",
)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pairs", type=int, default=3)
    parser.add_argument("--seconds", type=int, default=600)
    parser.add_argument("--max-retries", type=int, default=1)
    parser.add_argument("--emulator-host", default="127.0.0.1")
    parser.add_argument("--emulator-port", type=int, default=5555)
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


def run_one(root, result_dir, variant, args):
    result_dir.mkdir(parents=True, exist_ok=False)
    command = [
        args.python,
        "run_five_point_variant.py",
        "--variant",
        variant,
        "--connection-mode",
        "emulator",
        "--emulator-host",
        args.emulator_host,
        "--emulator-port",
        str(args.emulator_port),
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
        return {
            "variant": variant,
            "valid": False,
            "invalid_reason": "未生成 experiment_metrics.json",
            "returncode": process.returncode,
            "timed_out": timed_out,
            "result_dir": str(result_dir),
        }
    metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
    minimum_seconds = min(60, args.seconds * 0.5)
    valid = (
        process.returncode == 0
        and metrics.get("screenshots", 0) > 0
        and (
            timed_out
            or metrics.get("elapsed_seconds", 0) >= minimum_seconds
        )
    )
    metrics.update(
        {
            "valid": valid,
            "invalid_reason": (
                ""
                if valid
                else "运行过早结束、截图为0或进程返回码异常"
            ),
            "returncode": process.returncode,
            "timed_out": timed_out,
            "result_dir": str(result_dir),
        }
    )
    return metrics


def run_with_retries(root, result_root, pair, variant, args):
    attempts = []
    for attempt in range(1, args.max_retries + 2):
        result_dir = (
            result_root
            / f"pair_{pair}_{variant}_attempt_{attempt}"
        )
        result = run_one(root, result_dir, variant, args)
        result["pair"] = pair
        result["attempt"] = attempt
        attempts.append(result)
        if result["valid"]:
            return result, attempts
        print(
            f"[RETRY] pair={pair} variant={variant} "
            f"attempt={attempt} 无效：{result['invalid_reason']}"
        )
    raise RuntimeError(
        f"pair={pair} variant={variant} 连续运行无效；"
        f"查看 {attempts[-1]['result_dir']}/console.log"
    )


def aggregate(runs):
    result = {}
    for variant in ("baseline", "optimized"):
        selected = [
            run
            for run in runs
            if run["variant"] == variant and run["valid"]
        ]
        result[variant] = {
            name: statistics.median(
                float(run.get(name, 0)) for run in selected
            )
            for name in METRIC_NAMES
        }
    return result


def percentage_change(baseline, optimized):
    if baseline == 0:
        return "N/A"
    return f"{(optimized - baseline) / baseline:+.1%}"


def format_metric(name, value):
    if name == "near_duplicate_ratio":
        return f"{value:.1%}"
    return f"{value:.3f}"


def write_markdown(path, summary, args):
    baseline = summary["aggregate"]["baseline"]
    optimized = summary["aggregate"]["optimized"]
    labels = {
        "elapsed_seconds": "总耗时（秒）",
        "screenshots": "保存截图数",
        "screenshots_per_hour": "截图/小时",
        "near_unique_images_per_hour": "dHash 去近重后图片/小时",
        "near_duplicate_ratio": "dHash 近重复占比",
        "actions": "点击动作数",
        "actions_per_screenshot": "动作/截图",
        "unique_activities": "唯一 Activity",
        "exact_unique_images": "SHA-256 唯一图片",
        "near_unique_images_dhash12": "dHash 去近重后图片",
        "poco_attr_reads": "Poco 属性读取次数",
        "poco_attr_read_seconds": "Poco 属性读取耗时（秒）",
        "hierarchy_dumps": "Hierarchy dump 次数",
        "hierarchy_dump_seconds": "Hierarchy dump 耗时（秒）",
        "layout_duplicates_skipped": "UI dHash 跳过数",
        "image_duplicates_rejected": "截图 pHash 拒绝数",
        "capture_attempts": "截图尝试数",
        "capture_failures": "截图失败数",
        "capture_retry_attempts": "截图失败重试数",
        "capture_retry_recoveries": "截图重试成功数",
        "click_failures": "点击失败数",
        "parent_validation_failures": "父状态点击前校验失败数",
        "parent_restore_failures": "父状态恢复失败数",
        "fixed_wait_seconds": "固定等待耗时（秒）",
        "adaptive_wait_seconds": "自适应等待耗时（秒）",
        "state_wait_timeouts": "状态等待超时数",
    }
    lines = [
        "# 五项优化模拟器 AB/BA 实验",
        "",
        "- Baseline：旧五项行为",
        "- Optimized：只修改指定五项",
        "- 两组均固定：`MAX_DEPTH=1`",
        f"- 每组有效运行次数：{args.pairs}",
        f"- 每次最长运行：{args.seconds} 秒",
        "- 无效运行会自动重试且不参与中位数",
        "",
        "| 指标 | Baseline 中位数 | Optimized 中位数 | 变化 |",
        "|---|---:|---:|---:|",
    ]
    for name in METRIC_NAMES:
        first = baseline[name]
        second = optimized[name]
        lines.append(
            f"| {labels[name]} | {format_metric(name, first)} | "
            f"{format_metric(name, second)} | "
            f"{percentage_change(first, second)} |"
        )
    lines.extend(
        [
            "",
            "主结论优先看 `dHash 去近重后图片/小时`、"
            "`近重复占比`、`唯一 Activity` 和失败数。",
            "五个机制指标用于解释提升来自哪里，不能用原始截图数单独下结论。",
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")


def main():
    args = parse_args()
    if args.pairs < 1:
        raise SystemExit("--pairs 必须大于等于 1")
    if args.seconds < 60:
        raise SystemExit("--seconds 必须大于等于 60")
    if args.max_retries < 0:
        raise SystemExit("--max-retries 不能小于 0")

    root = Path(__file__).resolve().parent
    serial = emulator_preflight(args.emulator_host, args.emulator_port)
    result_root = Path(
        args.output
        or root
        / "five_point_experiment_results"
        / str(int(time.time()))
    ).resolve()
    result_root.mkdir(parents=True, exist_ok=False)

    runs = []
    attempts = []
    for pair in range(1, args.pairs + 1):
        order = (
            ("baseline", "optimized")
            if pair % 2
            else ("optimized", "baseline")
        )
        for variant in order:
            print(f"[RUN] pair={pair} variant={variant} MAX_DEPTH=1")
            result, run_attempts = run_with_retries(
                root,
                result_root,
                pair,
                variant,
                args,
            )
            runs.append(result)
            attempts.extend(run_attempts)

    summary = {
        "device": serial,
        "package": PACKAGE_NAME,
        "fixed_max_depth": 1,
        "pairs": args.pairs,
        "seconds_per_run": args.seconds,
        "runs": runs,
        "all_attempts": attempts,
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
