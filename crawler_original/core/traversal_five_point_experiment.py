"""五项遍历优化的隔离实验引擎。

两个模式都固定为父页 depth=0、子页 depth=1：

baseline:
  1. UI 布局 dHash 同时用于探索和截图去重
  2. 截图成功前写入访问指纹
  3. 每个节点逐项读取 Poco 属性
  4. 返回后复用旧 Poco 节点
  5. 固定 sleep 等待页面

optimized:
  1. 控件树 state key 负责探索，实际截图 pHash 负责交付去重
  2. 截图成功或确认近重复后才关闭截图状态
  3. 一次 hierarchy dump 提取动作
  4. 坐标动作 + 点击前父状态校验
  5. 轮询 UI 状态直到连续稳定

除这五点外，连接、Tab、截图、质量检查、Metadata 和回退入口均复用原版。
"""

from collections import Counter
import hashlib
import json
import os
import re
import subprocess
import time

import cv2
import numpy as np

from config import (
    BACK_WAIT,
    MAX_SCREENSHOTS,
    MAX_TOTAL_ACTIONS,
    MAX_RUNTIME_SECONDS,
    PACKAGE_NAME,
    PAGE_LOAD_WAIT,
    SKIP_ACTIVITY_KEYWORDS,
    SKIP_BLOCKED_POPUPS,
    SKIP_PERSONAL_PAGES,
    SKIP_TEXTS,
)
from core import fingerprint, metadata, popup_handler, privacy, screenshot
from core.adb_bin import ADB
from core import traversal as original_traversal


EXPERIMENT_MAX_DEPTH = 1
IMAGE_PHASH_THRESHOLD = 6
STATE_WAIT_TIMEOUT = 3.0
STATE_STABLE_INTERVAL = 0.15
STATE_STABLE_SAMPLES = 2
LIST_TYPES = {
    "RecyclerView",
    "ListView",
    "GridView",
    "ScrollView",
    "NestedScrollView",
    "HorizontalScrollView",
    "ViewPager",
    "ViewPager2",
}


def _normalize_text(value):
    value = " ".join(value.split()).strip().lower()
    value = re.sub(r"https?://\S+", "<url>", value)
    value = re.sub(r"\d+", "#", value)
    return value[:48]


def _quantize(value):
    try:
        return int(round(float(value) * 50))
    except (TypeError, ValueError):
        return 0


def state_key(hierarchy, activity):
    """生成探索状态键；不使用图片交付哈希。"""
    if not hierarchy:
        return ""
    normalized = []

    def walk(node, depth=0):
        if len(normalized) >= 500:
            return
        payload = node.get("payload", {})
        if not payload.get("visible", True):
            return
        pos = payload.get("pos") or [0, 0]
        size = payload.get("size") or [0, 0]
        normalized.append(
            [
                depth,
                payload.get("type") or "",
                (payload.get("name") or "").split("/")[-1],
                _normalize_text(
                    (payload.get("text") or "")
                    + " "
                    + (payload.get("desc") or "")
                ),
                _quantize(pos[0]),
                _quantize(pos[1]),
                _quantize(size[0]),
                _quantize(size[1]),
                bool(
                    payload.get("touchable")
                    or payload.get("clickable")
                ),
                bool(payload.get("selected")),
            ]
        )
        for child in node.get("children", []):
            walk(child, depth + 1)

    walk(hierarchy)
    encoded = json.dumps(
        normalized,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    digest = hashlib.blake2b(encoded, digest_size=16).hexdigest()
    return f"{activity}|{digest}"


def structure_key(hierarchy, activity):
    """生成忽略动态文本的结构签名，仅用于确认是否回到父模板。"""
    if not hierarchy:
        return ""
    normalized = []

    def walk(node, depth=0):
        if len(normalized) >= 500:
            return
        payload = node.get("payload", {})
        if not payload.get("visible", True):
            return
        pos = payload.get("pos") or [0, 0]
        size = payload.get("size") or [0, 0]
        normalized.append(
            [
                depth,
                payload.get("type") or "",
                (payload.get("name") or "").split("/")[-1],
                _quantize(pos[0]),
                _quantize(pos[1]),
                _quantize(size[0]),
                _quantize(size[1]),
                bool(
                    payload.get("touchable")
                    or payload.get("clickable")
                ),
                bool(payload.get("selected")),
            ]
        )
        if payload.get("type") in LIST_TYPES:
            return
        for child in node.get("children", []):
            walk(child, depth + 1)

    walk(hierarchy)
    encoded = json.dumps(
        normalized,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    digest = hashlib.blake2b(encoded, digest_size=16).hexdigest()
    return f"{activity}|{digest}"


def image_phash(path):
    """计算实际截图的 64-bit pHash。"""
    image = cv2.imread(path, cv2.IMREAD_GRAYSCALE)
    if image is None:
        return ""
    resized = cv2.resize(image, (32, 32), interpolation=cv2.INTER_AREA)
    dct = cv2.dct(np.float32(resized))
    values = dct[:8, :8].flatten()
    median = np.median(values[1:])
    bits = values > median
    value = 0
    for bit in bits:
        value = (value << 1) | int(bit)
    return f"{value:016x}"


def _hamming(first, second):
    try:
        return (int(first, 16) ^ int(second, 16)).bit_count()
    except (TypeError, ValueError):
        return 64


class FivePointExperimentTraversalEngine(
    original_traversal.TraversalEngine
):
    """在同一原版引擎上隔离比较五项修改。"""

    def __init__(self, *args, experiment_mode, **kwargs):
        if experiment_mode not in {"baseline", "optimized"}:
            raise ValueError("experiment_mode 必须是 baseline 或 optimized")
        self.experiment_mode = experiment_mode
        self.counters = Counter()
        self.timings = Counter()
        self.explored_state_keys = set()
        self.captured_state_keys = set()
        self.image_phashes = []
        self.failed_capture_states = Counter()
        self._active_depth = 0
        super().__init__(*args, **kwargs)

    def _metric_activity(self):
        started = time.monotonic()
        try:
            return metadata.get_current_activity(self.serial)
        finally:
            self.counters["activity_queries"] += 1
            self.timings["activity_query_seconds"] += (
                time.monotonic() - started
            )

    def _dump_hierarchy(self):
        started = time.monotonic()
        try:
            return super()._dump_hierarchy()
        finally:
            self.counters["hierarchy_dumps"] += 1
            self.timings["hierarchy_dump_seconds"] += (
                time.monotonic() - started
            )

    def _should_stop_experiment(self):
        return (
            self.screenshots_taken >= MAX_SCREENSHOTS
            or self.total_actions >= MAX_TOTAL_ACTIONS
            or time.monotonic() - self.run_started >= MAX_RUNTIME_SECONDS
        )

    def _explore_page(
        self,
        depth=0,
        parent_activities=None,
        path_states=None,
    ):
        del parent_activities, path_states
        if depth > EXPERIMENT_MAX_DEPTH or self._should_stop_experiment():
            return

        self._active_depth = depth
        activity = self._metric_activity()
        if not activity or PACKAGE_NAME not in activity:
            return
        if any(keyword in activity for keyword in SKIP_ACTIVITY_KEYWORDS):
            return

        if self.experiment_mode == "optimized":
            hierarchy = self._dump_hierarchy()
            if not hierarchy:
                return
            parent_state = state_key(hierarchy, activity)
            parent_structure = fingerprint.generate(hierarchy, activity)
            if parent_state in self.explored_state_keys:
                self.counters["known_states_skipped"] += 1
                return
            self._capture_optimized(
                activity,
                depth,
                hierarchy,
                parent_state,
            )
            if depth >= EXPERIMENT_MAX_DEPTH:
                return
            actions = self._actions_from_hierarchy(hierarchy)
        else:
            self._capture_baseline(activity, depth)
            if depth >= EXPERIMENT_MAX_DEPTH:
                return
            parent_state = ""
            parent_structure = ""
            actions = self._actions_from_poco()

        for action in actions:
            if self._should_stop_experiment():
                break

            if self.experiment_mode == "optimized":
                # 上一次回退已经校验过父模板；点击前只做轻量 Activity 校验，
                # 避免为每个动作额外执行一次完整 hierarchy dump。
                if self._metric_activity() != activity:
                    self.counters["parent_validation_failures"] += 1
                    break
                clicked = self._click_coordinates(action)
            else:
                clicked = self._click_poco_node(action)
            if not clicked:
                continue

            self.total_actions += 1
            self.counters["actions_clicked"] += 1

            if self.experiment_mode == "optimized":
                new_activity, new_hierarchy, new_state = (
                    self._wait_for_stable_state(parent_state)
                )
            else:
                self._fixed_wait(0.4)
                new_activity = self._metric_activity()
                new_hierarchy = None
                new_state = ""

            if not new_activity:
                continue
            if self.experiment_mode == "optimized":
                if not new_state or new_state == parent_state:
                    self.counters["actions_without_transition"] += 1
                    continue
            elif new_activity == activity:
                self.counters["actions_without_transition"] += 1
                continue

            if PACKAGE_NAME not in new_activity:
                self._handle_left_app()
                self._restore_parent(
                    activity,
                    parent_state,
                    parent_structure,
                    press_back=False,
                )
                continue
            if any(
                keyword in new_activity
                for keyword in SKIP_ACTIVITY_KEYWORDS
            ) or self._is_main_activity(new_activity):
                self._go_back()
                self._fixed_wait(BACK_WAIT)
                continue

            if self.experiment_mode == "optimized":
                dismissed = popup_handler.dismiss_popups(
                    self.poco,
                    max_attempts=2,
                )
                if dismissed:
                    new_activity, new_hierarchy, new_state = (
                        self._wait_for_stable_state(new_state)
                    )
                if new_hierarchy:
                    self._capture_optimized(
                        new_activity,
                        depth + 1,
                        new_hierarchy,
                        new_state,
                    )
            else:
                self._fixed_wait(max(PAGE_LOAD_WAIT - 0.4, 0))
                popup_handler.dismiss_popups(self.poco, max_attempts=2)
                self._capture_baseline(new_activity, depth + 1)

            self._restore_parent(
                activity,
                parent_state,
                parent_structure,
                current_state=new_state,
            )

    def _capture_baseline(self, activity, depth):
        hierarchy = self._dump_hierarchy()
        if not hierarchy:
            return False
        if SKIP_PERSONAL_PAGES and not self._is_main_activity(activity):
            if privacy.is_personal_page(hierarchy, activity):
                return False

        layout_fp = fingerprint.generate(hierarchy, activity)
        if fingerprint.find_similar(
            layout_fp,
            self.visited_fingerprints,
        ):
            self.counters["layout_duplicates_skipped"] += 1
            return False

        # 故意保留旧行为：截图成功前先写访问指纹。
        fingerprint.add_fingerprint(
            layout_fp,
            self.visited_fingerprints,
        )
        if (
            SKIP_BLOCKED_POPUPS
            and popup_handler.has_blocking_popup(self.poco)
        ):
            return False
        return self._save_capture(
            activity,
            depth,
            layout_fp,
            state="",
            use_image_dedupe=False,
        )

    def _capture_optimized(
        self,
        activity,
        depth,
        hierarchy,
        state,
    ):
        if state in self.captured_state_keys:
            return False
        if SKIP_PERSONAL_PAGES and not self._is_main_activity(activity):
            if privacy.is_personal_page(hierarchy, activity):
                self.explored_state_keys.add(state)
                return False
        if (
            SKIP_BLOCKED_POPUPS
            and popup_handler.has_blocking_popup(self.poco)
        ):
            self.explored_state_keys.add(state)
            return False

        layout_fp = fingerprint.generate(hierarchy, activity)
        return self._save_capture(
            activity,
            depth,
            layout_fp,
            state=state,
            use_image_dedupe=True,
        )

    def _save_capture(
        self,
        activity,
        depth,
        layout_fp,
        state,
        use_image_dedupe,
    ):
        self.counters["capture_attempts"] += 1
        if state and self.failed_capture_states[state]:
            self.counters["capture_retry_attempts"] += 1

        started = time.monotonic()
        path = screenshot.capture(
            self.serial,
            activity,
            layout_fp,
            segment_index=0,
        )
        self.timings["capture_seconds"] += time.monotonic() - started
        if not path:
            self.counters["capture_failures"] += 1
            if state:
                self.failed_capture_states[state] += 1
            return False

        if use_image_dedupe:
            phash = image_phash(path)
            if phash and any(
                _hamming(phash, existing) <= IMAGE_PHASH_THRESHOLD
                for existing in self.image_phashes
            ):
                os.remove(path)
                self.captured_state_keys.add(state)
                self.explored_state_keys.add(state)
                self.counters["image_duplicates_rejected"] += 1
                return False
        else:
            phash = ""

        record = metadata.build_record(
            path,
            activity,
            layout_fp,
            depth,
            self.device_info,
            self.app_info,
            segment_index=0,
        )
        metadata.append_record(record)
        if use_image_dedupe:
            self.image_phashes.append(phash)
            self.captured_state_keys.add(state)
            self.explored_state_keys.add(state)
            if self.failed_capture_states[state]:
                self.counters["capture_retry_recoveries"] += 1
        self.screenshots_taken += 1
        self.counters["screenshots_saved"] += 1
        elapsed = time.monotonic() - self.run_started
        print(
            f"  [{self.screenshots_taken}] "
            f"{activity.split('/')[-1]} ({elapsed:.0f}s)"
        )
        return True

    def _actions_from_poco(self):
        raw = []
        seen = set()
        try:
            nodes = self.poco(touchable=True)
            for node in nodes:
                try:
                    text = self._node_attr(node, "text") or ""
                    desc = self._node_attr(node, "desc") or ""
                    node_type = self._node_attr(node, "type") or ""
                    name = self._node_attr(node, "name") or ""
                    pos = self._node_attr(node, "pos")
                    action = self._make_action(
                        node_type,
                        name,
                        text,
                        desc,
                        pos,
                        node,
                        seen,
                    )
                    if action:
                        raw.append(action)
                except Exception:
                    self.counters["poco_node_read_failures"] += 1
        except Exception:
            return []
        return self._dedupe_actions(raw)

    def _node_attr(self, node, name):
        self.counters["poco_attr_reads"] += 1
        started = time.monotonic()
        try:
            return node.attr(name)
        finally:
            self.timings["poco_attr_read_seconds"] += (
                time.monotonic() - started
            )

    def _actions_from_hierarchy(self, hierarchy):
        raw = []
        seen = set()

        def walk(node):
            payload = node.get("payload", {})
            if not payload.get("visible", True):
                return
            if (
                payload.get("touchable")
                or payload.get("clickable")
            ) and payload.get("enabled", True):
                action = self._make_action(
                    payload.get("type") or "",
                    payload.get("name") or "",
                    payload.get("text") or "",
                    payload.get("desc")
                    or payload.get("description")
                    or "",
                    payload.get("pos"),
                    None,
                    seen,
                )
                if action:
                    raw.append(action)
            for child in node.get("children", []):
                walk(child)

        walk(hierarchy)
        return self._dedupe_actions(raw)

    def _make_action(
        self,
        node_type,
        name,
        text,
        desc,
        pos,
        node,
        seen,
    ):
        if not pos or len(pos) < 2:
            return None
        if not 0 <= pos[0] <= 1 or not 0 <= pos[1] <= 1:
            return None
        if any(keyword in text + desc for keyword in SKIP_TEXTS):
            return None
        if self._is_tab_node(name):
            return None
        action_id = self._action_id(node_type, name, text, pos)
        if action_id in seen:
            return None
        seen.add(action_id)
        return {
            "id": action_id,
            "node": node,
            "priority": self._priority(node_type, name, text),
            "x": pos[0],
            "y": pos[1],
        }

    @staticmethod
    def _dedupe_actions(raw):
        raw.sort(key=lambda action: action["priority"], reverse=True)
        result = []
        used = []
        for action in raw:
            if action["priority"] < 60:
                if any(
                    abs(action["y"] - y) < 0.02
                    and abs(action["x"] - x) < 0.1
                    for x, y in used
                ):
                    continue
                used.append((action["x"], action["y"]))
            result.append(action)
        return result

    def _click_poco_node(self, action):
        self.counters["click_attempts"] += 1
        try:
            action["node"].click()
            return True
        except Exception:
            self.counters["click_failures"] += 1
            return False

    def _click_coordinates(self, action):
        self.counters["click_attempts"] += 1
        try:
            x = int(action["x"] * self.screen_w)
            y = int(action["y"] * self.screen_h)
            result = subprocess.run(
                [
                    ADB,
                    "-s",
                    self.serial,
                    "shell",
                    "input",
                    "tap",
                    str(x),
                    str(y),
                ],
                capture_output=True,
                timeout=5,
            )
            if result.returncode == 0:
                return True
        except (KeyError, OSError, subprocess.SubprocessError):
            pass
        self.counters["click_failures"] += 1
        return False

    def _read_state(self):
        activity = self._metric_activity()
        hierarchy = self._dump_hierarchy()
        return activity, hierarchy, state_key(hierarchy, activity)

    def _wait_for_stable_state(self, previous_state):
        started = time.monotonic()
        deadline = started + STATE_WAIT_TIMEOUT
        previous_activity = previous_state.split("|", 1)[0]
        last_activity = ""
        activity_stable_samples = 0
        last_state = ""
        stable_samples = 0
        observation = ("", None, "")

        while time.monotonic() < deadline:
            activity = self._metric_activity()
            if activity and activity == last_activity:
                activity_stable_samples += 1
            else:
                last_activity = activity
                activity_stable_samples = 1

            # Activity 已变化且连续两次一致时，只需读取一次最终控件树。
            if (
                activity
                and activity != previous_activity
                and activity_stable_samples >= STATE_STABLE_SAMPLES
            ):
                hierarchy = self._dump_hierarchy()
                observation = (
                    activity,
                    hierarchy,
                    state_key(hierarchy, activity),
                )
                self.timings["adaptive_wait_seconds"] += (
                    time.monotonic() - started
                )
                return observation

            elapsed = time.monotonic() - started
            # Activity 不变时，低频检查控件树以识别 Fragment/Tab/弹窗。
            if elapsed >= 0.35:
                hierarchy = self._dump_hierarchy()
                current_state = state_key(hierarchy, activity)
                observation = (activity, hierarchy, current_state)
                if current_state and current_state == last_state:
                    stable_samples += 1
                else:
                    last_state = current_state
                    stable_samples = 1

                if (
                    current_state
                    and current_state != previous_state
                    and stable_samples >= STATE_STABLE_SAMPLES
                ):
                    self.timings["adaptive_wait_seconds"] += (
                        time.monotonic() - started
                    )
                    return observation

                # 无状态变化的动作不需要持续 dump 到超时。
                if elapsed >= min(PAGE_LOAD_WAIT, 0.5):
                    self.timings["adaptive_wait_seconds"] += (
                        time.monotonic() - started
                    )
                    return observation

            time.sleep(STATE_STABLE_INTERVAL)

        self.counters["state_wait_timeouts"] += 1
        self.timings["adaptive_wait_seconds"] += (
            time.monotonic() - started
        )
        return observation

    def _fixed_wait(self, seconds):
        if seconds <= 0:
            return
        started = time.monotonic()
        time.sleep(seconds)
        self.timings["fixed_wait_seconds"] += time.monotonic() - started

    def _is_parent_state(self, activity, parent_structure):
        current_activity, hierarchy, _ = self._read_state()
        current_structure = fingerprint.generate(
            hierarchy,
            current_activity,
        )
        return (
            current_activity == activity
            and fingerprint.is_same_page(
                current_structure,
                parent_structure,
            )
        )

    def _restore_parent(
        self,
        activity,
        state,
        parent_structure,
        press_back=True,
        current_state="",
    ):
        if self.experiment_mode == "baseline":
            self._go_back()
            self._fixed_wait(BACK_WAIT)
            now = self._metric_activity()
            if now != activity:
                success = self._back_to(activity)
                if not success:
                    self.counters["parent_restore_failures"] += 1
                return success
            return True

        if press_back:
            self._go_back()
            current_activity, hierarchy, current_state = (
                self._wait_for_stable_state(current_state or state)
            )
        else:
            current_activity, hierarchy, current_state = self._read_state()

        current_structure = fingerprint.generate(
            hierarchy,
            current_activity,
        )
        if (
            current_activity == activity
            and fingerprint.is_same_page(
                current_structure,
                parent_structure,
            )
        ):
            return True

        for _ in range(3):
            self._go_back()
            current_activity, hierarchy, current_state = (
                self._wait_for_stable_state(current_state)
            )
            current_structure = fingerprint.generate(
                hierarchy,
                current_activity,
            )
            if (
                current_activity == activity
                and fingerprint.is_same_page(
                    current_structure,
                    parent_structure,
                )
            ):
                return True
            if not current_activity or PACKAGE_NAME not in current_activity:
                break
        self.counters["parent_restore_failures"] += 1
        return False

    def experiment_metrics(self):
        return {
            "experiment_mode": self.experiment_mode,
            "configured_max_depth": EXPERIMENT_MAX_DEPTH,
            "screenshots": self.screenshots_taken,
            "actions": self.total_actions,
            "explored_states": len(self.explored_state_keys),
            "captured_states": len(self.captured_state_keys),
            "counters": dict(sorted(self.counters.items())),
            "timings": {
                name: round(value, 6)
                for name, value in sorted(self.timings.items())
            },
        }
