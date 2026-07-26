"""B 站实测用最小 Demo：同口径比较 baseline 与 optimized。

baseline:
  - FIFO 顺序
  - 固定等待
  - URL 去重 + 截图后 dHash
  - 最多探索父页和一层子页

optimized:
  - 有界 Best-First Frontier
  - 自适应页面稳定等待
  - URL 家族 + 规范化 DOM 状态去重
  - SHA-256 + pHash 截图去重

两种模式共用同一截图、质量检查、Metadata 和指标代码，避免统计口径不同。
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import time
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright

from algorithm_engine import (
    BaselineFrontier,
    BestFirstFrontier,
    CrawlTask,
    ImageIndex,
    compute_dhash,
    compute_phash,
    image_quality,
    normalize_url,
    state_id,
    url_family,
)


VIEWPORT = {"width": 1920, "height": 1080}


class Metrics:
    def __init__(self, mode: str):
        self.mode = mode
        self.started = time.monotonic()
        self.counters = Counter()
        self.timings = defaultdict(list)
        self.errors = []
        self.url_families = set()
        self.states = set()
        self.max_depth_visited = 0

    def observe(self, name: str, seconds: float):
        self.timings[name].append(seconds)

    def finish(self) -> dict:
        elapsed = max(time.monotonic() - self.started, 1e-9)
        saved = self.counters["screenshots_saved"]
        loaded = self.counters["pages_loaded"]
        result = {
            "mode": self.mode,
            "elapsed_seconds": elapsed,
            "counters": dict(self.counters),
            "unique_states": len(self.states),
            "unique_url_families": len(self.url_families),
            "max_depth_visited": self.max_depth_visited,
            "errors": self.errors,
            "timings": {},
            "rates": {
                "pages_per_minute": loaded * 60 / elapsed,
                "screenshots_per_hour": saved * 3600 / elapsed,
                "saved_per_loaded_page": saved / loaded if loaded else 0.0,
                "states_per_loaded_page": (
                    len(self.states) / loaded if loaded else 0.0
                ),
            },
        }
        for name, values in self.timings.items():
            result["timings"][name] = {
                "count": len(values),
                "total_seconds": sum(values),
                "average_seconds": sum(values) / len(values),
                "max_seconds": max(values),
            }
        return result


def load_tasks(filepath: str) -> list[CrawlTask]:
    tasks = []
    with open(filepath, "r", encoding="utf-8") as source:
        for row in csv.DictReader(source):
            url = row.get("url", "").strip()
            if url:
                tasks.append(CrawlTask(
                    url=url,
                    label=row.get("category", ""),
                ))
    return tasks


async def wait_baseline():
    """原 Demo 采用固定等待。"""
    await asyncio.sleep(2.0)


async def wait_until_stable(page, timeout: float = 5.0) -> float:
    """连续两次观测一致后返回，避免固定 sleep 的过等和欠等。"""
    started = time.monotonic()
    deadline = started + timeout
    last = None
    stable_count = 0

    while time.monotonic() < deadline:
        sample = await page.evaluate("""
            () => {
                const body = document.body;
                const images = Array.from(document.images);
                return {
                    ready: document.readyState,
                    height: body ? Math.round(body.scrollHeight / 50) : 0,
                    text: body ? Math.round(body.innerText.length / 100) : 0,
                    children: body ? body.children.length : 0,
                    pendingImages: images.filter(i => !i.complete).length
                };
            }
        """)
        stable_count = stable_count + 1 if sample == last else 1
        last = sample
        if (
            stable_count >= 2
            and sample["ready"] in ("interactive", "complete")
            and sample["pendingImages"] == 0
        ):
            break
        await asyncio.sleep(0.25)
    return time.monotonic() - started


async def dismiss_common_overlays(page):
    selectors = [
        ".bili-mini-close-icon",
        ".login-panel-close",
        '[aria-label="关闭"]',
        '[aria-label="Close"]',
    ]
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            if await locator.is_visible(timeout=200):
                await locator.click(timeout=500)
        except Exception:
            continue


async def dom_signature(page) -> dict:
    """只保留布局和语义角色，忽略动态文字、时间和计数。"""
    return await page.evaluate("""
        () => {
            const visible = (el) => {
                const r = el.getBoundingClientRect();
                const s = getComputedStyle(el);
                return r.width > 4 && r.height > 4 &&
                       s.display !== 'none' && s.visibility !== 'hidden';
            };
            const nodes = Array.from(document.querySelectorAll(
                'header,nav,main,section,article,aside,footer,' +
                'a,button,input,[role],[class*="card"],[class*="grid"]'
            )).filter(visible).slice(0, 400);
            return {
                titleClass: (document.title || '').replace(/\\d+/g, '#').slice(0, 80),
                scrollHeight: Math.round((document.body?.scrollHeight || 0) / 100),
                nodes: nodes.map(el => {
                    const r = el.getBoundingClientRect();
                    return [
                        el.tagName.toLowerCase(),
                        el.getAttribute('role') || '',
                        (el.id || '').replace(/\\d+/g, '#').slice(0, 30),
                        Math.round(r.x / 20),
                        Math.round(r.y / 20),
                        Math.round(r.width / 20),
                        Math.round(r.height / 20)
                    ];
                })
            };
        }
    """)


async def discover_links(page, base_url: str, limit: int) -> list[CrawlTask]:
    base_domain = urlparse(base_url).netloc.lower()
    raw_links = await page.eval_on_selector_all(
        "a[href]",
        """(links) => links.map(a => ({
            href: a.href,
            label: (a.innerText || a.getAttribute('aria-label') || '').trim().slice(0, 60)
        }))""",
    )
    result = []
    seen = set()
    for link in raw_links:
        url = normalize_url(link.get("href", ""))
        if (
            not url.startswith(("http://", "https://"))
            or urlparse(url).netloc.lower() != base_domain
            or url in seen
        ):
            continue
        seen.add(url)
        result.append(CrawlTask(url=url, label=link.get("label", "")))
        if len(result) >= limit:
            break
    return result


async def run(args) -> dict:
    output_dir = Path(args.output)
    screenshot_dir = output_dir / "screenshots"
    screenshot_dir.mkdir(parents=True, exist_ok=True)
    metadata_path = output_dir / "metadata.csv"

    metrics = Metrics(args.mode)
    frontier = (
        BaselineFrontier(max_depth=1)
        if args.mode == "baseline"
        else BestFirstFrontier(max_depth=args.max_depth)
    )
    for task in load_tasks(args.input):
        frontier.push(task)

    image_index = ImageIndex(
        mode=args.mode,
        threshold=10 if args.mode == "baseline" else 6,
    )
    visited_urls = set()
    visited_states = set()
    records = []

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=not args.headed)
        context = await browser.new_context(
            viewport=VIEWPORT,
            locale="zh-CN",
            device_scale_factor=1,
        )
        page = await context.new_page()

        while (
            frontier
            and metrics.counters["pages_attempted"] < args.max_pages
            and time.monotonic() - metrics.started < args.max_runtime
        ):
            task = frontier.pop()
            metrics.max_depth_visited = max(
                metrics.max_depth_visited, task.depth
            )
            url = normalize_url(task.url)
            if url in visited_urls:
                metrics.counters["url_duplicates_skipped"] += 1
                continue
            visited_urls.add(url)
            metrics.counters["pages_attempted"] += 1

            load_started = time.monotonic()
            try:
                await page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=args.timeout_ms,
                )
                metrics.observe("page_load", time.monotonic() - load_started)
                metrics.counters["pages_loaded"] += 1
            except PlaywrightTimeoutError as error:
                metrics.observe("page_load", time.monotonic() - load_started)
                metrics.counters["load_failures"] += 1
                metrics.errors.append({"url": url, "error": str(error)[:160]})
                continue
            except Exception as error:
                metrics.observe("page_load", time.monotonic() - load_started)
                metrics.counters["load_failures"] += 1
                metrics.errors.append({"url": url, "error": str(error)[:160]})
                continue

            wait_started = time.monotonic()
            if args.mode == "baseline":
                await wait_baseline()
            else:
                await wait_until_stable(page)
            metrics.observe("state_wait", time.monotonic() - wait_started)
            await dismiss_common_overlays(page)

            signature_started = time.monotonic()
            signature = await dom_signature(page)
            current_state = state_id(page.url, signature)
            metrics.observe(
                "state_fingerprint", time.monotonic() - signature_started
            )
            metrics.states.add(current_state)
            metrics.url_families.add(url_family(page.url))

            if args.mode == "optimized" and current_state in visited_states:
                metrics.counters["state_duplicates_skipped"] += 1
                continue
            visited_states.add(current_state)

            capture_started = time.monotonic()
            image_bytes = await page.screenshot(
                type="png",
                animations="disabled",
                full_page=False,
            )
            metrics.observe("screenshot", time.monotonic() - capture_started)
            metrics.counters["screenshots_attempted"] += 1

            passed, reason = image_quality(image_bytes)
            if not passed:
                metrics.counters[f"quality_rejected_{reason}"] += 1
                continue

            is_new, sha256, hash_or_reason = image_index.add_if_new(image_bytes)
            if not is_new:
                metrics.counters[hash_or_reason] += 1
                continue

            perceptual_hash = (
                compute_dhash(image_bytes)
                if args.mode == "baseline"
                else compute_phash(image_bytes)
            )
            filename = (
                f"{metrics.counters['screenshots_saved'] + 1:04d}_"
                f"{sha256[:12]}.png"
            )
            filepath = screenshot_dir / filename
            filepath.write_bytes(image_bytes)
            metrics.counters["screenshots_saved"] += 1

            records.append({
                "mode": args.mode,
                "file_name": filename,
                "source_url": page.url,
                "depth": task.depth,
                "state_id": current_state,
                "url_family": url_family(page.url),
                "sha256": sha256,
                "perceptual_hash": perceptual_hash,
            })

            if args.discover and task.depth < frontier.max_depth:
                links = await discover_links(
                    page, page.url, args.max_links_per_page
                )
                for link in links:
                    if frontier.push(CrawlTask(
                        url=link.url,
                        depth=task.depth + 1,
                        parent=page.url,
                        label=link.label,
                    )):
                        metrics.counters["links_enqueued"] += 1

            if args.request_interval > 0:
                await asyncio.sleep(args.request_interval)

        await context.close()
        await browser.close()

    fields = [
        "mode", "file_name", "source_url", "depth", "state_id",
        "url_family", "sha256", "perceptual_hash",
    ]
    with open(metadata_path, "w", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(output, fieldnames=fields)
        writer.writeheader()
        writer.writerows(records)

    result = metrics.finish()
    result["config"] = {
        "input": os.path.abspath(args.input),
        "max_pages": args.max_pages,
        "max_depth": frontier.max_depth,
        "max_runtime": args.max_runtime,
        "discover": args.discover,
        "viewport": VIEWPORT,
    }
    with open(output_dir / "metrics.json", "w", encoding="utf-8") as output:
        json.dump(result, output, ensure_ascii=False, indent=2)
    return result


def parse_args():
    parser = argparse.ArgumentParser(
        description="同口径比较原始与优化遍历策略"
    )
    parser.add_argument(
        "--mode", choices=("baseline", "optimized"), required=True
    )
    parser.add_argument(
        "--input", default="input/bilibili_validation_urls.csv"
    )
    parser.add_argument("--output", required=True)
    parser.add_argument("--max-pages", type=int, default=10)
    parser.add_argument("--max-depth", type=int, default=3)
    parser.add_argument("--max-runtime", type=float, default=300)
    parser.add_argument("--timeout-ms", type=int, default=20000)
    parser.add_argument("--max-links-per-page", type=int, default=20)
    parser.add_argument("--request-interval", type=float, default=0.5)
    parser.add_argument("--discover", action="store_true")
    parser.add_argument("--headed", action="store_true")
    return parser.parse_args()


if __name__ == "__main__":
    arguments = parse_args()
    summary = asyncio.run(run(arguments))
    print(json.dumps(summary, ensure_ascii=False, indent=2))
