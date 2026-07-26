# 最终算法优化 Demo

本目录包含两种验证入口：

1. `main.py`：Android 状态感知遍历，需要真机和 B 站 App；
2. `optimized_demo.py`：Web 最小对照 Demo，不需要 Android 手机，可直接用 B 站网页产生实测数据。

所有遍历都有深度、页面数和运行时间上限。这里采用的是“有界状态图探索”，不是无上限 BFS。

## Web 最小 Demo

### 安装

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements_demo.txt
playwright install chromium
```

### 同口径运行

先清空旧输出：

```bash
rm -rf output/demo_baseline output/demo_optimized
```

运行原算法对照：

```bash
python optimized_demo.py \
  --mode baseline \
  --input input/bilibili_validation_urls.csv \
  --output output/demo_baseline \
  --max-pages 10 \
  --max-runtime 300
```

运行最终优化算法：

```bash
python optimized_demo.py \
  --mode optimized \
  --input input/bilibili_validation_urls.csv \
  --output output/demo_optimized \
  --max-pages 10 \
  --max-depth 3 \
  --max-runtime 300
```

生成对比报告：

```bash
python compare_results.py \
  --baseline output/demo_baseline/metrics.json \
  --optimized output/demo_optimized/metrics.json \
  --output output/comparison.md
```

两次运行必须使用相同机器、网络、URL、视口、页面上限和无缓存浏览器上下文。

增加 `--discover` 后会从页面继续发现同域链接：

- `baseline` 使用 FIFO，最多父页和一层子页；
- `optimized` 使用 Best-First Frontier，默认最多 3 层；
- 两者仍受 `--max-pages` 和 `--max-runtime` 硬限制，不会无限遍历。

### 输出

每种模式生成：

- `screenshots/`：实际网页截图；
- `metadata.csv`：URL、深度、状态 ID、图片哈希；
- `metrics.json`：加载、等待、截图、状态覆盖和去重指标。

重点比较：

- `elapsed_seconds`
- `unique_states`
- `unique_url_families`
- `rates.pages_per_minute`
- `rates.screenshots_per_hour`
- `rates.saved_per_loaded_page`
- `timings.state_wait.average_seconds`
- 重复拒绝数和加载失败数

### 当前实测结果

固定 URL、5 页、2 组 AB/BA 的中位数：

| 指标 | Baseline | Optimized | 变化 |
|---|---:|---:|---:|
| 总耗时 | 17.100 秒 | 13.816 秒 | -19.2% |
| 页面/分钟 | 17.548 | 21.718 | +23.8% |
| 平均稳定等待 | 2.001 秒 | 1.541 秒 | -23.0% |

结果保存在 `output/ab_validation/`。

链接发现烟测的结果保存在 `output/ab_discovery_smoke/`。该烟测每种模式
处理 10 页，优化版加入前沿 68 个链接，对照版为 19 个；正式覆盖结论仍应
按 3 组 AB/BA 复测。

## Android 状态图 Demo

### 环境

- Python 3.10-3.12
- ADB
- 一台已安装并登录 B 站的 Android 手机
- USB 调试授权

```bash
python3.12 -m venv .venv-android
source .venv-android/bin/activate
pip install -r requirements.txt
adb devices -l
python main.py
```

Android 版本使用：

```text
Activity + 规范化 UI 控件树 = state_id
Activity 优先、控件树按需采样的状态稳定检测
默认 Depth 1 的横向优先状态图探索
模板识别和父状态容错恢复
探索状态与实际图片 pHash 分离
```

Android 默认采用 Depth 1，是因为 B站模拟器实测中 Depth 2/3 的回退和重启
成本高于新增有效图收益。深度 2/3 保留为按 App Pilot 后启用的配置，不作为
全局默认。

正式 2 组 AB/BA、每次 180 秒的中位数：

| 指标 | Original | Optimized | 变化 |
|---|---:|---:|---:|
| 截图/小时 | 249.6 | 399.6 | +60.1% |
| dHash 去近重图片/小时 | 219.6 | 399.6 | +82.0% |
| 唯一 Activity | 7.5 | 9.0 | +20.0% |
| App 重启次数 | 3.0 | 0.5 | -83.3% |

结果位于：

```text
../android_original_optimized_results/final_abba_2x180/
```

运行结果：

- `output/screenshots/`
- `output/metadata.csv`
- `output/state.json`
- `output/run_metrics.json`

## 离线测试

Web 算法组件：

```bash
python -m unittest -v test_algorithm_engine.py
```

Android 组件：

```bash
python -m unittest discover -v
```
