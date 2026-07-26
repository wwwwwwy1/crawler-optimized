"""优化算法最小 Demo 的离线测试。"""

import io
import unittest

import numpy as np
from PIL import Image

from algorithm_engine import (
    BaselineFrontier,
    BestFirstFrontier,
    CrawlTask,
    ImageIndex,
    compute_phash,
    hamming_distance,
    normalize_url,
    state_id,
    url_family,
)


def image_bytes(brightness=0):
    pixels = np.zeros((720, 1280, 3), dtype=np.uint8)
    pixels[100:620, 100:1180] = [40, 120, 220]
    pixels[240:480, 300:980] = [220, 180, 50]
    pixels = np.clip(
        pixels.astype(np.int16) + brightness, 0, 255
    ).astype(np.uint8)
    output = io.BytesIO()
    Image.fromarray(pixels).save(output, format="PNG")
    return output.getvalue()


class FrontierTests(unittest.TestCase):
    def test_baseline_keeps_fifo_and_rejects_grandchild(self):
        frontier = BaselineFrontier(max_depth=1)
        self.assertTrue(frontier.push(CrawlTask("https://example.com/a")))
        self.assertTrue(frontier.push(CrawlTask("https://example.com/b", depth=1)))
        self.assertFalse(
            frontier.push(CrawlTask("https://example.com/c", depth=2))
        )
        self.assertEqual("https://example.com/a", frontier.pop().url)
        self.assertEqual("https://example.com/b", frontier.pop().url)

    def test_best_first_prioritizes_high_value_new_family(self):
        frontier = BestFirstFrontier(max_depth=3)
        frontier.push(CrawlTask("https://example.com/profile/1", label="profile"))
        frontier.push(CrawlTask("https://example.com/c/game", label="游戏频道"))
        self.assertEqual(
            "https://example.com/c/game",
            frontier.pop().url,
        )


class FingerprintTests(unittest.TestCase):
    def test_url_normalization_removes_tracking_parameters(self):
        normalized = normalize_url(
            "HTTPS://WWW.BILIBILI.COM/video/BV123/?spm_id_from=1&p=2#comment"
        )
        self.assertEqual(
            "https://www.bilibili.com/video/BV123?p=2",
            normalized,
        )
        self.assertEqual(
            "www.bilibili.com/video/{id}",
            url_family(normalized),
        )

    def test_state_id_changes_with_layout(self):
        first = state_id(
            "https://www.bilibili.com/c/game",
            {"nodes": [["main", 0, 0, 10, 10]]},
        )
        second = state_id(
            "https://www.bilibili.com/c/game",
            {"nodes": [["main", 0, 0, 20, 10]]},
        )
        self.assertNotEqual(first, second)

    def test_phash_tolerates_small_brightness_change(self):
        first = compute_phash(image_bytes(0))
        second = compute_phash(image_bytes(4))
        self.assertLessEqual(hamming_distance(first, second), 6)

    def test_image_index_rejects_near_duplicate(self):
        index = ImageIndex(mode="optimized", threshold=6)
        self.assertTrue(index.add_if_new(image_bytes(0))[0])
        is_new, _, reason = index.add_if_new(image_bytes(4))
        self.assertFalse(is_new)
        self.assertEqual("near_duplicate", reason)


if __name__ == "__main__":
    unittest.main()
