# Web 算法 AB/BA 对比

- 每种模式运行次数：2
- 每次页面上限：5

| 指标 | Baseline 中位数 | Optimized 中位数 | 变化 |
|---|---:|---:|---:|
| `elapsed_seconds` | 17.100 | 13.816 | -19.2% |
| `rates.pages_per_minute` | 17.548 | 21.718 | +23.8% |
| `rates.screenshots_per_hour` | 1052.906 | 1303.059 | +23.8% |
| `rates.saved_per_loaded_page` | 1.000 | 1.000 | +0.0% |
| `timings.page_load.average_seconds` | 0.608 | 0.440 | -27.7% |
| `timings.state_wait.average_seconds` | 2.001 | 1.541 | -23.0% |
| `timings.screenshot.average_seconds` | 0.169 | 0.150 | -11.2% |
| `unique_states` | 5.000 | 5.000 | +0.0% |
| `unique_url_families` | 5.000 | 5.000 | +0.0% |
| `counters.screenshots_saved` | 5.000 | 5.000 | +0.0% |
| `counters.load_failures` | 0.000 | 0.000 | N/A |