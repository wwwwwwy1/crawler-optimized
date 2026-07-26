"""无需设备的指纹与近重复性能基准。"""

import json
import random
import time

from core import fingerprint, image_dedupe


def make_node(node_type, x=0.5, y=0.5, w=1.0, h=1.0, children=None, **extra):
    payload = {
        "type": node_type,
        "visible": True,
        "pos": [x, y],
        "size": [w, h],
    }
    payload.update(extra)
    return {"payload": payload, "children": children or []}


def measure_lookup(hash_bits, count, lookup):
    hashes = [
        f"{random.getrandbits(hash_bits):0{hash_bits // 4}x}"
        for _ in range(count)
    ]
    query = f"{random.getrandbits(hash_bits):0{hash_bits // 4}x}"
    started = time.perf_counter()
    lookup(query, hashes)
    return (time.perf_counter() - started) * 1000


def main():
    random.seed(20260725)
    layouts = []
    states = []
    started = time.perf_counter()

    for index in range(1000):
        children = []
        for child_index in range(5):
            children.append(
                make_node(
                    random.choice(["TextView", "ImageView", "Button"]),
                    random.random(),
                    random.random(),
                    0.05 + random.random() * 0.4,
                    0.03 + random.random() * 0.2,
                    text=f"item-{index}-{child_index}",
                )
            )
        hierarchy = make_node(
            "Root",
            children=[
                make_node("RecyclerView", 0.5, 0.5, 1.0, 0.8, children)
            ],
        )
        layouts.append(fingerprint.generate(hierarchy, "pkg/Main"))
        states.append(fingerprint.state_key(hierarchy, "pkg/Main"))

    result = {
        "list_samples": 1000,
        "layout_unique": len(set(layouts)),
        "state_unique": len(set(states)),
        "layout_collision_rate": 1 - len(set(layouts)) / len(layouts),
        "generation_elapsed_ms": (time.perf_counter() - started) * 1000,
        "layout_lookup_ms": {},
        "phash_lookup_ms": {},
    }

    for count in (100, 1000, 10000, 100000):
        result["layout_lookup_ms"][str(count)] = measure_lookup(
            256,
            count,
            lambda query, hashes: fingerprint.find_similar(
                f"pkg/Main|{query}", {"pkg/Main": hashes}
            ),
        )
        result["phash_lookup_ms"][str(count)] = measure_lookup(
            64,
            count,
            lambda query, hashes: image_dedupe.find_similar(query, hashes, 6),
        )

    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
