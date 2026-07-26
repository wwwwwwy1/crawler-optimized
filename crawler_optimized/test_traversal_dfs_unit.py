"""自动遍历、去重、截图和 Metadata 的离线回归测试。"""

import os
import subprocess
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import cv2
import numpy as np

from config import PACKAGE_NAME
from core import fingerprint, image_dedupe, metadata, screenshot
from core.run_metrics import RunMetrics
from core.traversal import TraversalEngine


ACTIVITY = f"{PACKAGE_NAME}/IndexActivity"


def make_node(
    node_type,
    *,
    name="",
    text="",
    pos=(0.5, 0.5),
    size=(0.2, 0.1),
    touchable=False,
    children=None,
):
    return {
        "payload": {
            "type": node_type,
            "name": name,
            "text": text,
            "visible": True,
            "enabled": True,
            "touchable": touchable,
            "pos": list(pos),
            "size": list(size),
        },
        "children": children or [],
    }


def make_page(label, children=None):
    return make_node(
        "Root",
        name=f"root_{label}",
        size=(1.0, 1.0),
        children=children,
    )


class FakeHierarchy:
    def __init__(self, hierarchy):
        self.value = hierarchy

    def dump(self):
        return self.value


class FakePoco:
    def __init__(self, hierarchy):
        self.agent = Mock()
        self.agent.hierarchy = FakeHierarchy(hierarchy)


def make_engine(hierarchy):
    return TraversalEngine(
        FakePoco(hierarchy),
        "test-serial",
        {
            "device_model": "test",
            "android_version": "14",
            "screen_resolution": "1080x2400",
        },
        {
            "package_name": PACKAGE_NAME,
            "app_name": "test",
            "version_name": "1",
        },
    )


class FingerprintTests(unittest.TestCase):
    def test_list_children_affect_layout_and_state_fingerprints(self):
        first = make_page(
            "feed",
            [make_node(
                "RecyclerView",
                size=(1.0, 0.8),
                children=[make_node("TextView", text="alpha", pos=(0.2, 0.2))],
            )],
        )
        second = make_page(
            "feed",
            [make_node(
                "RecyclerView",
                size=(1.0, 0.8),
                children=[make_node("Button", text="beta", pos=(0.8, 0.7))],
            )],
        )

        self.assertNotEqual(
            fingerprint.generate(first, ACTIVITY),
            fingerprint.generate(second, ACTIVITY),
        )
        self.assertNotEqual(
            fingerprint.state_key(first, ACTIVITY),
            fingerprint.state_key(second, ACTIVITY),
        )

    def test_hamming_distance_supports_256_bit_hashes(self):
        self.assertEqual(256, fingerprint._hamming_distance("0" * 64, "f" * 64))

    def test_template_key_ignores_dynamic_list_content(self):
        first = make_page(
            "feed",
            [make_node(
                "RecyclerView",
                name="feed_list",
                size=(1.0, 0.8),
                children=[make_node("TextView", text="视频 A")],
            )],
        )
        second = make_page(
            "feed",
            [make_node(
                "RecyclerView",
                name="feed_list",
                size=(1.0, 0.8),
                children=[make_node("Button", text="视频 B")],
            )],
        )

        self.assertEqual(
            fingerprint.template_key(first, ACTIVITY),
            fingerprint.template_key(second, ACTIVITY),
        )
        self.assertNotEqual(
            fingerprint.state_key(first, ACTIVITY),
            fingerprint.state_key(second, ACTIVITY),
        )


class ImageDedupeTests(unittest.TestCase):
    def test_phash_matches_small_brightness_change(self):
        with tempfile.TemporaryDirectory() as directory:
            base = np.zeros((1000, 720, 3), dtype=np.uint8)
            cv2.rectangle(base, (80, 100), (640, 900), (220, 120, 20), -1)
            brighter = cv2.convertScaleAbs(base, alpha=1.0, beta=4)
            first = os.path.join(directory, "first.png")
            second = os.path.join(directory, "second.png")
            cv2.imwrite(first, base)
            cv2.imwrite(second, brighter)

            first_hash = image_dedupe.compute_phash(first)
            second_hash = image_dedupe.compute_phash(second)
            self.assertLessEqual(
                image_dedupe.hamming_distance(first_hash, second_hash), 6
            )


class ScreenshotTests(unittest.TestCase):
    def test_capture_uses_single_screencap_command(self):
        image = np.tile(
            np.arange(1080, dtype=np.uint8), (1920, 1)
        )
        image = cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
        ok, encoded = cv2.imencode(".png", image)
        self.assertTrue(ok)
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=encoded.tobytes(), stderr=b""
        )

        with tempfile.TemporaryDirectory() as directory, patch.object(
            screenshot, "SCREENSHOT_DIR", directory
        ), patch.object(
            screenshot.subprocess, "run", return_value=completed
        ) as run, patch.object(
            screenshot, "_get_status_bar_height", return_value=60
        ), patch.object(
            screenshot, "_get_nav_bar_height", return_value=80
        ):
            path = screenshot.capture(
                "serial", ACTIVITY, f"{ACTIVITY}|{'a' * 64}"
            )

        self.assertIsNotNone(path)
        self.assertEqual(1, run.call_count)
        self.assertIn("exec-out", run.call_args.args[0])


class MetadataTests(unittest.TestCase):
    def test_build_record_contains_required_delivery_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            filepath = os.path.join(directory, "image.png")
            cv2.imwrite(
                filepath,
                np.full((1000, 720, 3), 127, dtype=np.uint8),
            )
            record = metadata.build_record(
                filepath,
                ACTIVITY,
                "ui-fingerprint",
                2,
                {
                    "device_model": "test",
                    "screen_resolution": "720x1280",
                    "android_version": "14",
                },
                {
                    "package_name": PACKAGE_NAME,
                    "app_name": "test",
                    "version_name": "1",
                },
                image_phash="0" * 16,
            )

        required = {
            "image_id", "platform", "source_url", "language", "category",
            "design_style", "source_batch", "image_sha256", "image_phash",
        }
        self.assertTrue(required.issubset(record))
        self.assertEqual(record["image_id"], record["image_sha256"])


class TraversalTests(unittest.TestCase):
    def test_actions_are_extracted_from_one_hierarchy_dump(self):
        hierarchy = make_page(
            "home",
            [
                make_node(
                    "Button",
                    name="open_detail",
                    text="详情",
                    pos=(0.5, 0.4),
                    touchable=True,
                ),
                make_node(
                    "Button",
                    name="publish",
                    text="发布",
                    pos=(0.5, 0.6),
                    touchable=True,
                ),
            ],
        )
        engine = make_engine(hierarchy)

        actions = engine._get_actions(hierarchy)

        self.assertEqual(1, len(actions))
        self.assertEqual("Button:open_detail@0.5,0.4", actions[0]["id"])

    def test_same_activity_state_transition_is_explored(self):
        action = {
            "id": "Button:detail",
            "priority": 40,
            "x": 0.5,
            "y": 0.5,
        }
        parent = make_page("parent")
        child = make_page("child")
        child_state = fingerprint.state_key(child, ACTIVITY)
        engine = make_engine(parent)
        engine._get_current_activity = Mock(return_value=ACTIVITY)
        engine._try_screenshot = Mock(return_value=True)
        engine._get_actions = Mock(side_effect=[[action], []])
        engine._back_to_state = Mock(return_value=True)
        engine._click = Mock(return_value=True)
        engine._wait_for_stable_state = Mock(
            side_effect=[
                (ACTIVITY, child, child_state),
                (ACTIVITY, child, child_state),
            ]
        )

        with patch(
            "core.traversal.popup_handler.dismiss_popups", return_value=0
        ):
            engine._explore_page(depth=0, initial_hierarchy=parent)

        self.assertEqual(
            1, engine.metrics.counters["same_activity_transitions"]
        )
        captured_states = [
            call.args[3] for call in engine._try_screenshot.call_args_list
        ]
        self.assertIn(child_state, captured_states)

    def test_deep_action_budget_decreases_with_depth(self):
        engine = make_engine(make_page("home"))
        actions = [{"id": str(index)} for index in range(10)]

        self.assertEqual(10, len(engine._limit_actions_for_depth(actions, 0)))
        self.assertEqual(2, len(engine._limit_actions_for_depth(actions, 1)))
        self.assertEqual(1, len(engine._limit_actions_for_depth(actions, 2)))

    def test_parent_restore_accepts_same_activity_after_one_back(self):
        parent = make_page("parent")
        changed_parent = make_page("parent_changed")
        engine = make_engine(parent)
        engine._go_back = Mock()
        engine._wait_for_stable_state = Mock(
            return_value=(
                ACTIVITY,
                changed_parent,
                fingerprint.state_key(changed_parent, ACTIVITY),
            )
        )

        restored = engine._back_to_state(
            ACTIVITY,
            fingerprint.state_key(parent, ACTIVITY),
            fingerprint.template_key(parent, ACTIVITY),
            current_state="child-state",
        )

        self.assertTrue(restored)
        self.assertEqual(1, engine._go_back.call_count)
        self.assertEqual(
            1,
            engine.metrics.counters[
                "parent_template_mismatch_accepted"
            ],
        )


class MetricsTests(unittest.TestCase):
    def test_snapshot_calculates_effectiveness_rates(self):
        metrics = RunMetrics()
        metrics.started_monotonic = time.monotonic() - 10
        metrics.inc("actions_attempted", 4)
        metrics.inc("transitions_detected", 2)
        metrics.inc("capture_attempts", 2)
        metrics.inc("screenshots_saved", 1)

        rates = metrics.snapshot()["rates"]

        self.assertEqual(0.5, rates["transition_rate_per_action"])
        self.assertEqual(0.5, rates["capture_success_rate"])


if __name__ == "__main__":
    unittest.main()
