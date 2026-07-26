"""运行一次五项优化隔离实验。"""

import argparse
import csv
import hashlib
import json
import subprocess
import time
from pathlib import Path

import cv2

import config


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--variant",
        choices=("baseline", "optimized"),
        required=True,
    )
    parser.add_argument(
        "--connection-mode",
        choices=("usb", "wifi", "emulator"),
        default="emulator",
    )
    parser.add_argument("--emulator-host", default="127.0.0.1")
    parser.add_argument("--emulator-port", type=int, default=5555)
    parser.add_argument("--output-dir", required=True)
    return parser.parse_args()


def configure_runtime(args):
    output_dir = Path(args.output_dir).resolve()
    if output_dir.exists() and any(output_dir.iterdir()):
        raise SystemExit(f"输出目录非空，拒绝覆盖：{output_dir}")
    screenshot_dir = output_dir / "screenshots"
    screenshot_dir.mkdir(parents=True, exist_ok=True)

    config.CONNECTION_MODE = args.connection_mode
    config.EMULATOR_HOST = args.emulator_host
    config.EMULATOR_PORT = args.emulator_port
    config.OUTPUT_DIR = str(output_dir)
    config.SCREENSHOT_DIR = str(screenshot_dir)
    config.METADATA_CSV = str(output_dir / "metadata.csv")
    config.STATE_FILE = str(output_dir / "state.json")
    return output_dir


def launch_app(adb, serial):
    subprocess.run(
        [
            adb,
            "-s",
            serial,
            "shell",
            "am",
            "force-stop",
            config.PACKAGE_NAME,
        ],
        capture_output=True,
        timeout=5,
        check=False,
    )
    time.sleep(1)
    subprocess.run(
        [
            adb,
            "-s",
            serial,
            "shell",
            "monkey",
            "-p",
            config.PACKAGE_NAME,
            "-c",
            "android.intent.category.LAUNCHER",
            "1",
        ],
        capture_output=True,
        timeout=5,
        check=False,
    )
    time.sleep(4)


def image_dhash(path):
    image = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
    if image is None:
        return None
    resized = cv2.resize(image, (17, 16), interpolation=cv2.INTER_AREA)
    bits = resized[:, 1:] > resized[:, :-1]
    value = 0
    for bit in bits.flatten():
        value = (value << 1) | int(bit)
    return value


def count_near_unique(paths, threshold=12):
    representatives = []
    for path in paths:
        value = image_dhash(path)
        if value is None:
            continue
        if any(
            (value ^ known).bit_count() <= threshold
            for known in representatives
        ):
            continue
        representatives.append(value)
    return len(representatives)


def output_metrics(output_dir, engine, elapsed, args, serial):
    screenshots = sorted((output_dir / "screenshots").glob("*.png"))
    exact_hashes = {
        hashlib.sha256(path.read_bytes()).hexdigest()
        for path in screenshots
    }
    activities = set()
    metadata_rows = 0
    metadata_path = output_dir / "metadata.csv"
    if metadata_path.exists():
        with metadata_path.open("r", encoding="utf-8") as source:
            for row in csv.DictReader(source):
                metadata_rows += 1
                if row.get("activity_name"):
                    activities.add(row["activity_name"])

    near_unique = count_near_unique(screenshots)
    engine_metrics = engine.experiment_metrics()
    counters = engine_metrics["counters"]
    timings = engine_metrics["timings"]
    metrics = {
        **engine_metrics,
        "variant": args.variant,
        "device": serial,
        "package": config.PACKAGE_NAME,
        "elapsed_seconds": elapsed,
        "metadata_rows": metadata_rows,
        "unique_activities": len(activities),
        "exact_unique_images": len(exact_hashes),
        "near_unique_images_dhash12": near_unique,
        "screenshots_per_hour": (
            len(screenshots) * 3600 / max(elapsed, 1e-9)
        ),
        "near_unique_images_per_hour": (
            near_unique * 3600 / max(elapsed, 1e-9)
        ),
        "near_duplicate_ratio": (
            1 - near_unique / max(len(screenshots), 1)
        ),
        "actions_per_screenshot": (
            engine.total_actions / max(len(screenshots), 1)
        ),
        "poco_attr_reads": counters.get("poco_attr_reads", 0),
        "poco_attr_read_seconds": timings.get(
            "poco_attr_read_seconds",
            0,
        ),
        "hierarchy_dumps": counters.get("hierarchy_dumps", 0),
        "hierarchy_dump_seconds": timings.get(
            "hierarchy_dump_seconds",
            0,
        ),
        "layout_duplicates_skipped": counters.get(
            "layout_duplicates_skipped",
            0,
        ),
        "image_duplicates_rejected": counters.get(
            "image_duplicates_rejected",
            0,
        ),
        "capture_attempts": counters.get("capture_attempts", 0),
        "capture_failures": counters.get("capture_failures", 0),
        "capture_retry_attempts": counters.get(
            "capture_retry_attempts",
            0,
        ),
        "capture_retry_recoveries": counters.get(
            "capture_retry_recoveries",
            0,
        ),
        "click_failures": counters.get("click_failures", 0),
        "parent_validation_failures": counters.get(
            "parent_validation_failures",
            0,
        ),
        "parent_restore_failures": counters.get(
            "parent_restore_failures",
            0,
        ),
        "fixed_wait_seconds": timings.get("fixed_wait_seconds", 0),
        "adaptive_wait_seconds": timings.get(
            "adaptive_wait_seconds",
            0,
        ),
        "state_wait_timeouts": counters.get(
            "state_wait_timeouts",
            0,
        ),
    }
    (output_dir / "experiment_metrics.json").write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return metrics


def main():
    args = parse_args()
    output_dir = configure_runtime(args)

    # 配置独立输出路径后再导入依赖模块。
    from core.adb_bin import ADB
    from core.device import connect
    from core.metadata import get_app_info, get_device_info, init_csv
    from core.popup_handler import handle_onboarding
    from core.traversal_five_point_experiment import (
        FivePointExperimentTraversalEngine,
    )

    print(
        f"[FIVE_POINT_EXPERIMENT] variant={args.variant}, "
        f"MAX_DEPTH=1, output={output_dir}"
    )
    _, poco, serial = connect()
    device_info = get_device_info(serial)
    app_info = get_app_info(serial, config.PACKAGE_NAME)
    init_csv()
    launch_app(ADB, serial)
    handle_onboarding(poco)
    time.sleep(1)

    engine = FivePointExperimentTraversalEngine(
        poco,
        serial,
        device_info,
        app_info,
        resume=False,
        experiment_mode=args.variant,
    )
    started = time.monotonic()
    try:
        engine.run()
    finally:
        elapsed = time.monotonic() - started
        metrics = output_metrics(
            output_dir,
            engine,
            elapsed,
            args,
            serial,
        )
        print(json.dumps(metrics, ensure_ascii=False, indent=2))
        print(f"[FIVE_POINT_EXPERIMENT_DONE] {output_dir}")


if __name__ == "__main__":
    main()
