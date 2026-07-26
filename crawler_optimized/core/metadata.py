"""元数据提取模块"""

import csv
import os
import re
import subprocess
import time
from datetime import datetime

import cv2

from config import (
    DEFAULT_CATEGORY,
    DEFAULT_DESIGN_STYLE,
    DEFAULT_LANGUAGE,
    METADATA_CSV,
    SOURCE_BATCH,
)
from core.adb_bin import ADB
from core.image_dedupe import sha256_file


# CSV 表头
FIELDS = [
    "schema_version", "image_id", "file_name", "platform",
    "package_name", "app_name", "version_name", "source_url",
    "activity_name", "page_fingerprint", "segment_index", "depth",
    "language", "category", "design_style", "source_batch",
    "image_sha256", "image_phash", "image_width", "image_height",
    "device_model", "screen_resolution", "android_version", "capture_time",
]
SCHEMA_VERSION = "2"


def init_csv():
    """初始化 CSV 文件（写入表头）"""
    if os.path.exists(METADATA_CSV):
        with open(METADATA_CSV, "r", newline="", encoding="utf-8") as f:
            current_fields = next(csv.reader(f), [])
        if current_fields == FIELDS:
            return
        backup = f"{METADATA_CSV}.schema-v1-{int(time.time())}.bak"
        os.replace(METADATA_CSV, backup)

    with open(METADATA_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()


def append_record(record: dict):
    """追加一条记录到 CSV"""
    with open(METADATA_CSV, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        writer.writerow(record)


def get_current_activity(serial: str) -> str:
    """获取当前前台 Activity"""
    try:
        result = subprocess.run(
            [ADB, "-s", serial, "shell", "dumpsys", "activity", "activities"],
            capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    for line in result.stdout.split("\n"):
        if "mResumedActivity" in line or "topResumedActivity" in line:
            match = re.search(r"([\w.]+/[\w.]+)", line)
            if match:
                return match.group(1)
    return ""


def get_device_info(serial: str) -> dict:
    """获取设备基本信息（启动时调用一次）"""
    def _prop(key):
        r = subprocess.run(
            [ADB, "-s", serial, "shell", "getprop", key],
            capture_output=True, text=True, timeout=5
        )
        return r.stdout.strip()

    def _shell(cmd):
        r = subprocess.run(
            [ADB, "-s", serial, "shell"] + cmd.split(),
            capture_output=True, text=True, timeout=5
        )
        return r.stdout.strip()

    resolution = ""
    wm_output = _shell("wm size")
    match = re.search(r"(\d+x\d+)", wm_output)
    if match:
        resolution = match.group(1)

    return {
        "device_model": _prop("ro.product.model"),
        "android_version": _prop("ro.build.version.release"),
        "screen_resolution": resolution,
    }


def get_app_info(serial: str, package_name: str) -> dict:
    """获取 App 信息"""
    result = subprocess.run(
        [ADB, "-s", serial, "shell", "dumpsys", "package", package_name],
        capture_output=True, text=True, timeout=10
    )
    info = {"package_name": package_name, "app_name": package_name, "version_name": ""}

    for line in result.stdout.split("\n"):
        line = line.strip()
        if "versionName=" in line:
            info["version_name"] = line.split("versionName=")[-1].strip()
            break

    return info


def build_record(
    screenshot_path: str,
    activity: str,
    fingerprint: str,
    depth: int,
    device_info: dict,
    app_info: dict,
    segment_index: int = 0,
    image_phash: str = "",
    image_sha256: str = "",
) -> dict:
    """组装一条完整的元数据记录"""
    if not image_sha256:
        image_sha256 = sha256_file(screenshot_path)
    image = cv2.imread(screenshot_path)
    image_height, image_width = image.shape[:2] if image is not None else (0, 0)

    return {
        "schema_version": SCHEMA_VERSION,
        "image_id": image_sha256,
        "file_name": os.path.basename(screenshot_path),
        "platform": "app",
        "package_name": app_info["package_name"],
        "app_name": app_info["app_name"],
        "version_name": app_info["version_name"],
        "source_url": "",
        "activity_name": activity,
        "page_fingerprint": fingerprint,
        "segment_index": segment_index,
        "depth": depth,
        "language": DEFAULT_LANGUAGE,
        "category": DEFAULT_CATEGORY,
        "design_style": DEFAULT_DESIGN_STYLE,
        "source_batch": SOURCE_BATCH,
        "image_sha256": image_sha256,
        "image_phash": image_phash,
        "image_width": image_width,
        "image_height": image_height,
        "device_model": device_info["device_model"],
        "screen_resolution": device_info["screen_resolution"],
        "android_version": device_info["android_version"],
        "capture_time": datetime.now().isoformat(),
    }
