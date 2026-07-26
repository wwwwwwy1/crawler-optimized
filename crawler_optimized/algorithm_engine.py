"""优化 Demo 的纯算法组件，不依赖浏览器，便于离线测试。"""

from __future__ import annotations

import hashlib
import heapq
import io
import json
import re
from collections import deque
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import numpy as np
from PIL import Image


@dataclass(frozen=True)
class CrawlTask:
    url: str
    depth: int = 0
    parent: str = ""
    label: str = ""


class BaselineFrontier:
    """原算法对照：按发现顺序 FIFO，深度上限为 1。"""

    def __init__(self, max_depth: int = 1):
        self.max_depth = max_depth
        self._queue = deque()
        self._queued = set()

    def push(self, task: CrawlTask) -> bool:
        normalized = normalize_url(task.url)
        if task.depth > self.max_depth or normalized in self._queued:
            return False
        self._queued.add(normalized)
        self._queue.append(task)
        return True

    def pop(self) -> CrawlTask:
        return self._queue.popleft()

    def __bool__(self):
        return bool(self._queue)


class BestFirstFrontier:
    """最终算法：在有界前沿中优先选择预计信息增益更高的任务。"""

    def __init__(self, max_depth: int = 3):
        self.max_depth = max_depth
        self._heap = []
        self._queued = set()
        self._path_families = set()
        self._sequence = 0

    def push(self, task: CrawlTask) -> bool:
        normalized = normalize_url(task.url)
        if task.depth > self.max_depth or normalized in self._queued:
            return False
        self._queued.add(normalized)

        family = url_family(normalized)
        novelty = 1.0 if family not in self._path_families else 0.0
        self._path_families.add(family)
        score = (
            6.0 * novelty
            + 3.0 * _semantic_value(task.url, task.label)
            - 0.8 * task.depth
            - 0.2 * len(urlparse(task.url).query)
        )
        self._sequence += 1
        heapq.heappush(self._heap, (-score, self._sequence, task))
        return True

    def pop(self) -> CrawlTask:
        return heapq.heappop(self._heap)[2]

    def __bool__(self):
        return bool(self._heap)


def normalize_url(url: str) -> str:
    """移除片段和追踪参数，保留会改变页面内容的查询参数。"""
    parsed = urlparse(url.strip())
    filtered_query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if not key.lower().startswith(("utm_", "spm_", "from", "vd_source"))
    ]
    path = re.sub(r"/+", "/", parsed.path or "/").rstrip("/") or "/"
    return urlunparse((
        parsed.scheme.lower() or "https",
        parsed.netloc.lower(),
        path,
        "",
        urlencode(filtered_query),
        "",
    ))


def url_family(url: str) -> str:
    """将动态 ID 归一化，得到用于衡量覆盖度的 URL 家族。"""
    parsed = urlparse(url)
    segments = []
    for segment in parsed.path.split("/"):
        if not segment:
            continue
        if re.fullmatch(r"\d{5,}|BV[A-Za-z0-9]+|[0-9a-f]{12,}", segment):
            segments.append("{id}")
        else:
            segments.append(segment.lower())
    return parsed.netloc.lower() + "/" + "/".join(segments[:3])


def state_id(url: str, dom_signature: dict) -> str:
    """页面地址家族与规范化 DOM 共同构成探索状态。"""
    payload = {
        "url_family": url_family(url),
        "dom": dom_signature,
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.blake2b(encoded, digest_size=16).hexdigest()


def exact_hash(image_bytes: bytes) -> str:
    return hashlib.sha256(image_bytes).hexdigest()


def compute_dhash(image_bytes: bytes, hash_size: int = 16) -> str:
    image = Image.open(io.BytesIO(image_bytes)).convert("L")
    image = image.resize((hash_size + 1, hash_size), Image.Resampling.LANCZOS)
    pixels = np.asarray(image)
    bits = (pixels[:, 1:] > pixels[:, :-1]).flatten()
    return _bits_to_hex(bits)


def compute_phash(image_bytes: bytes, hash_size: int = 8) -> str:
    """使用二维 DCT 计算 64-bit pHash。"""
    sample_size = hash_size * 4
    image = Image.open(io.BytesIO(image_bytes)).convert("L")
    image = image.resize((sample_size, sample_size), Image.Resampling.LANCZOS)
    pixels = np.asarray(image, dtype=np.float64)

    transform = _dct_matrix(sample_size)
    dct = transform @ pixels @ transform.T
    # 理论上整体亮度变化只影响 DC 分量。四舍五入用于消除手写 DCT
    # 中接近 0 的浮点误差，避免零系数符号抖动导致大量 bit 翻转。
    low_frequency = np.round(
        dct[:hash_size, :hash_size], decimals=6
    ).flatten()
    median = np.median(low_frequency[1:])
    return _bits_to_hex(low_frequency > median)


def hamming_distance(first: str, second: str) -> int:
    if len(first) != len(second):
        return max(len(first), len(second)) * 4
    try:
        return (int(first, 16) ^ int(second, 16)).bit_count()
    except ValueError:
        return max(len(first), len(second)) * 4


@dataclass
class ImageIndex:
    """Demo 规模的内存索引；生产环境应替换为 Faiss/HNSW。"""

    mode: str
    threshold: int
    exact_hashes: set[str] = field(default_factory=set)
    perceptual_hashes: list[str] = field(default_factory=list)

    def add_if_new(self, image_bytes: bytes) -> tuple[bool, str, str]:
        sha256 = exact_hash(image_bytes)
        if sha256 in self.exact_hashes:
            return False, sha256, "exact_duplicate"

        perceptual = (
            compute_dhash(image_bytes)
            if self.mode == "baseline"
            else compute_phash(image_bytes)
        )
        if any(
            hamming_distance(perceptual, existing) <= self.threshold
            for existing in self.perceptual_hashes
        ):
            return False, sha256, "near_duplicate"

        self.exact_hashes.add(sha256)
        self.perceptual_hashes.append(perceptual)
        return True, sha256, perceptual


def image_quality(image_bytes: bytes) -> tuple[bool, str]:
    image = Image.open(io.BytesIO(image_bytes)).convert("L")
    pixels = np.asarray(image)
    if image.width < 1280 or image.height < 720:
        return False, "low_resolution"
    if float(pixels.std()) < 10.0:
        return False, "low_variance"
    return True, ""


def _semantic_value(url: str, label: str) -> float:
    content = (url + " " + label).lower()
    high_value = (
        "anime", "movie", "documentary", "game", "music",
        "knowledge", "dance", "channel", "category",
    )
    return 1.0 if any(keyword in content for keyword in high_value) else 0.4


def _bits_to_hex(bits) -> str:
    value = 0
    for bit in bits:
        value = (value << 1) | int(bool(bit))
    width = (len(bits) + 3) // 4
    return f"{value:0{width}x}"


_DCT_CACHE = {}


def _dct_matrix(size: int) -> np.ndarray:
    if size in _DCT_CACHE:
        return _DCT_CACHE[size]
    x = np.arange(size)
    k = np.arange(size)[:, None]
    matrix = np.cos(np.pi * (2 * x + 1) * k / (2 * size))
    matrix[0, :] *= np.sqrt(1 / size)
    matrix[1:, :] *= np.sqrt(2 / size)
    _DCT_CACHE[size] = matrix.astype(np.float64)
    return _DCT_CACHE[size]
