# Web 算法 AB/BA 对比

- 每种模式运行次数：1
- 每次页面上限：10

| 指标 | Baseline 中位数 | Optimized 中位数 | 变化 |
|---|---:|---:|---:|
| `elapsed_seconds` | 33.022 | 23.163 | -29.9% |
| `rates.pages_per_minute` | 18.170 | 25.903 | +42.6% |
| `rates.screenshots_per_hour` | 1090.177 | 1554.197 | +42.6% |
| `rates.saved_per_loaded_page` | 1.000 | 1.000 | +0.0% |
| `timings.page_load.average_seconds` | 0.522 | 0.441 | -15.7% |
| `timings.state_wait.average_seconds` | 2.001 | 1.127 | -43.7% |
| `timings.screenshot.average_seconds` | 0.162 | 0.139 | -13.6% |
| `unique_states` | 10.000 | 10.000 | +0.0% |
| `unique_url_families` | 10.000 | 10.000 | +0.0% |
| `counters.screenshots_saved` | 10.000 | 10.000 | +0.0% |
| `counters.load_failures` | 0.000 | 0.000 | N/A |