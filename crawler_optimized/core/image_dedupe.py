"""基于实际截图内容的轻量级近重复检测。"""

import hashlib

import cv2
import numpy as np


def compute_phash(filepath: str) -> str:
    """计算 64-bit pHash，读取失败时返回空字符串。"""
    image = cv2.imread(filepath, cv2.IMREAD_GRAYSCALE)
    if image is None:
        return ""

    resized = cv2.resize(image, (32, 32), interpolation=cv2.INTER_AREA)
    dct = cv2.dct(np.float32(resized))
    low_frequency = dct[:8, :8]
    values = low_frequency.flatten()
    median = np.median(values[1:])
    bits = values > median

    value = 0
    for bit in bits:
        value = (value << 1) | int(bit)
    return f"{value:016x}"


def find_similar(phash: str, existing_hashes: list, threshold: int) -> bool:
    """判断是否存在汉明距离不大于 threshold 的实际截图。"""
    if not phash:
        return False
    for existing in existing_hashes:
        if hamming_distance(phash, existing) <= threshold:
            return True
    return False


def hamming_distance(hash1: str, hash2: str) -> int:
    if len(hash1) != len(hash2):
        return max(len(hash1), len(hash2)) * 4
    try:
        return (int(hash1, 16) ^ int(hash2, 16)).bit_count()
    except ValueError:
        return max(len(hash1), len(hash2)) * 4


def sha256_file(filepath: str) -> str:
    digest = hashlib.sha256()
    with open(filepath, "rb") as image_file:
        for chunk in iter(lambda: image_file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
