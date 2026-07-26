"""状态感知的自动遍历引擎。

策略:
  - Activity + 规范化控件树共同标识探索状态
  - 从单次 hierarchy dump 收集动作，按坐标执行，避免节点属性 RPC
  - 自适应等待状态稳定，并支持同 Activity 状态转移
  - 探索防环与实际截图 pHash 去重分离
  - 按 MAX_DEPTH 有界深入，回退时校验父状态
"""

import json
import os
import subprocess
import time
from collections import Counter
from datetime import datetime

from core import (
    fingerprint,
    image_dedupe,
    metadata,
    popup_handler,
    privacy,
    screenshot,
)
from core.adb_bin import ADB
from core.run_metrics import RunMetrics
from config import (
    IMAGE_PHASH_THRESHOLD,
    DEPTH_1_ACTION_LIMIT,
    DEPTH_2_ACTION_LIMIT,
    LIST_ITEM_MAX_CLICK,
    PACKAGE_NAME,
    MAIN_ACTIVITY_KEYWORD,
    MAX_DEPTH,
    MAX_RUNTIME_SECONDS,
    MAX_SCREENSHOTS,
    MAX_STATES,
    MAX_SAME_TEMPLATE_STREAK,
    MAX_TOTAL_ACTIONS,
    SCROLL_MAX_TIMES,
    SKIP_PERSONAL_PAGES,
    SKIP_BLOCKED_POPUPS,
    SKIP_TEXTS,
    SKIP_ACTIVITY_KEYWORDS,
    PAGE_LOAD_WAIT,
    BACK_WAIT,
    RUN_METRICS_FILE,
    STATE_FILE,
    STATE_STABLE_INTERVAL,
    STATE_STABLE_SAMPLES,
    STATE_WAIT_TIMEOUT,
)

TAB_ID_KEYWORDS = ("tab", "bottom_nav", "navigation")


class TraversalEngine:

    def __init__(self, poco, serial, device_info, app_info, resume=False):
        self.poco = poco
        self.serial = serial
        self.device_info = device_info
        self.app_info = app_info

        self.visited_fingerprints = {}
        self.explored_state_keys = set()
        self.captured_state_keys = set()
        self.image_phashes = []
        self.template_expansions = Counter()
        self.completed_tabs = set()
        self.screenshots_taken = 0
        self.total_actions = 0
        self.run_started = time.monotonic()
        self.metrics = RunMetrics()
        self.metrics.set_context(
            serial=serial,
            device=device_info,
            app=app_info,
            limits={
                "max_depth": MAX_DEPTH,
                "max_screenshots": MAX_SCREENSHOTS,
                "max_states": MAX_STATES,
                "max_total_actions": MAX_TOTAL_ACTIONS,
                "max_runtime_seconds": MAX_RUNTIME_SECONDS,
                "scroll_max_times": SCROLL_MAX_TIMES,
                "image_phash_threshold": IMAGE_PHASH_THRESHOLD,
            },
        )

        if resume:
            self._load_state()

        self.screen_w, self.screen_h = self._parse_resolution(
            device_info.get("screen_resolution", "")
        )

    @staticmethod
    def _parse_resolution(res):
        try:
            w, h = res.lower().split("x")
            return int(w), int(h)
        except Exception:
            return 1080, 2400

    # ------------------------------------------------------------------
    # 状态持久化
    # ------------------------------------------------------------------

    def _load_state(self):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                state = json.load(f)
            self.visited_fingerprints = state.get("visited_fingerprints", {})
            self.explored_state_keys = set(state.get("explored_state_keys", []))
            self.captured_state_keys = set(state.get("captured_state_keys", []))
            self.image_phashes = list(state.get("image_phashes", []))
            self.completed_tabs = set(state.get("completed_tabs", []))
            self.screenshots_taken = state.get("screenshots_taken", 0)
            self.total_actions = state.get("total_actions", 0)
            fp_count = sum(len(v) for v in self.visited_fingerprints.values())
            print(f"[INFO] 恢复: {self.screenshots_taken}张, "
                  f"{len(self.completed_tabs)}Tab完成, {fp_count}指纹")
        except (FileNotFoundError, json.JSONDecodeError):
            pass

    def _save_state(self):
        state = {
            "visited_fingerprints": self.visited_fingerprints,
            "explored_state_keys": list(self.explored_state_keys),
            "captured_state_keys": list(self.captured_state_keys),
            "image_phashes": self.image_phashes,
            "completed_tabs": list(self.completed_tabs),
            "screenshots_taken": self.screenshots_taken,
            "total_actions": self.total_actions,
            "timestamp": datetime.now().isoformat(),
        }
        try:
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------

    def run(self):
        print("[INFO] 开始遍历...")
        try:
            popup_handler.dismiss_popups(self.poco)
            self._ensure_on_main_page()

            tab_count = self._count_valid_tabs()
            if tab_count > 0:
                print(f"[INFO] 有效Tab: {tab_count}\n")

                # 每个Tab: 广度探索(子页面进入即探索)
                for i in range(tab_count):
                    if self.screenshots_taken >= MAX_SCREENSHOTS:
                        break
                    if i in self.completed_tabs:
                        continue
                    if self._run_tab(i):
                        self.completed_tabs.add(i)
                    self._save_state()

                # 滚动后再探索一轮
                if self.screenshots_taken < MAX_SCREENSHOTS:
                    print("\n[INFO] 滚动探索\n")
                    for i in range(tab_count):
                        if self.screenshots_taken >= MAX_SCREENSHOTS:
                            break
                        self._scroll_tab(i)
                    self._save_state()
            else:
                self._ensure_on_main_page()
                self._explore_page(depth=0)
        except KeyboardInterrupt:
            print("\n[INFO] 中断, 保存状态...")
        finally:
            self._save_state()
            self.metrics.write(RUN_METRICS_FILE)
            elapsed = time.monotonic() - self.run_started
            m, s = divmod(elapsed, 60)
            print(f"\n[DONE] 截图 {self.screenshots_taken} 张, 用时 {int(m)}分{s:.0f}秒")

        return self.screenshots_taken

    # ------------------------------------------------------------------
    # Tab
    # ------------------------------------------------------------------

    def _run_tab(self, order):
        try:
            self._ensure_on_main_page()
            time.sleep(1.0)
            if not self._switch_tab(order):
                print(f"[TAB {order}] 切换失败, 跳过")
                return False
            print(f"[TAB {order}] 开始")
            self._explore_page(depth=0)
            return True
        except Exception as e:
            print(f"[TAB {order}] 异常: {e}")
            return False

    def _scroll_tab(self, order):
        try:
            self._ensure_on_main_page()
            time.sleep(0.5)
            if not self._switch_tab(order):
                return
            previous_state = ""
            for _ in range(SCROLL_MAX_TIMES):
                if self.screenshots_taken >= MAX_SCREENSHOTS:
                    return
                self._swipe(0.7, 0.3)
                activity, hierarchy, state = self._wait_for_stable_state(previous_state)
                if not activity or PACKAGE_NAME not in activity:
                    break
                if state and state == previous_state:
                    break
                previous_state = state
                self._explore_page(depth=0, initial_hierarchy=hierarchy)
        except Exception:
            pass

    def _switch_tab(self, order):
        tabs = self._find_tabs()
        valid = [t for t in tabs if not self._is_publish_button(t)]
        if order >= len(valid):
            print(f"  [DEBUG] Tab {order}: 超出有效Tab数({len(valid)})")
            return False
        try:
            valid[order].click()
            time.sleep(PAGE_LOAD_WAIT)
            popup_handler.dismiss_popups(self.poco, max_attempts=2)
            activity = metadata.get_current_activity(self.serial)
            if not activity or PACKAGE_NAME not in activity:
                print(f"  [DEBUG] Tab {order}: 不在App内, activity={activity}")
                return False
            if any(kw in activity for kw in SKIP_ACTIVITY_KEYWORDS):
                print(f"  [DEBUG] Tab {order}: 命中SKIP_ACTIVITY, activity={activity}")
                return False
            if SKIP_PERSONAL_PAGES:
                h = self._dump_hierarchy()
                if h and privacy.is_personal_page(h, activity):
                    print(f"  [DEBUG] Tab {order}: 被判定为个人页, activity={activity}")
                    return False
            return True
        except Exception as e:
            print(f"  [DEBUG] Tab {order}: 异常 {e}")
            return False

    # ------------------------------------------------------------------
    # 核心: 探索当前页面
    # ------------------------------------------------------------------

    def _should_stop(self):
        return (
            self.screenshots_taken >= MAX_SCREENSHOTS
            or len(self.explored_state_keys) >= MAX_STATES
            or self.total_actions >= MAX_TOTAL_ACTIONS
            or time.monotonic() - self.run_started >= MAX_RUNTIME_SECONDS
        )

    def _explore_page(
        self,
        depth=0,
        initial_hierarchy=None,
        path_templates=(),
    ):
        """探索当前状态，并在 MAX_DEPTH 内继续探索新状态。"""
        if depth > MAX_DEPTH or self._should_stop():
            return

        activity = self._get_current_activity()
        hierarchy = initial_hierarchy or self._dump_hierarchy()
        if not activity or PACKAGE_NAME not in activity or not hierarchy:
            return
        if any(kw in activity for kw in SKIP_ACTIVITY_KEYWORDS):
            return

        parent_state = fingerprint.state_key(hierarchy, activity)
        parent_template = fingerprint.template_key(hierarchy, activity)
        if not parent_state or parent_state in self.explored_state_keys:
            return
        self.explored_state_keys.add(parent_state)
        self.metrics.inc("states_explored")

        self._try_screenshot(activity, depth, hierarchy, parent_state)
        if depth >= MAX_DEPTH or self._should_stop():
            self.metrics.inc("depth_limit_hits")
            return

        actions = self._limit_actions_for_depth(
            self._get_actions(hierarchy),
            depth,
        )

        for action in actions:
            if self._should_stop():
                break
            if not self._back_to_state(
                activity,
                parent_state,
                parent_template,
                press_back=False,
            ):
                self.metrics.inc("parent_restore_failures")
                break

            self.metrics.inc("actions_attempted")
            if not self._click(action):
                self.metrics.inc("action_click_failures")
                continue
            self.total_actions += 1
            self.metrics.inc("actions_clicked")

            new_activity, new_hierarchy, new_state = self._wait_for_stable_state(
                parent_state
            )
            if not new_activity or not new_hierarchy or not new_state:
                self.metrics.inc("state_read_failures")
                continue
            if new_state == parent_state:
                self.metrics.inc("actions_without_transition")
                continue

            self.metrics.inc("transitions_detected")
            if new_activity == activity:
                self.metrics.inc("same_activity_transitions")

            if PACKAGE_NAME not in new_activity:
                self.metrics.inc("external_transitions")
                self._handle_left_app()
                if not self._back_to_state(
                    activity,
                    parent_state,
                    parent_template,
                    press_back=False,
                ):
                    break
                continue

            if any(kw in new_activity for kw in SKIP_ACTIVITY_KEYWORDS):
                self.metrics.inc("dangerous_states_skipped")
                if not self._back_to_state(
                    activity,
                    parent_state,
                    parent_template,
                    current_state=new_state,
                ):
                    break
                continue

            dismissed = popup_handler.dismiss_popups(
                self.poco,
                max_attempts=2,
            )
            if dismissed:
                new_activity, new_hierarchy, new_state = (
                    self._wait_for_stable_state(new_state)
                )
            if not new_hierarchy:
                if not self._back_to_state(
                    activity,
                    parent_state,
                    parent_template,
                    current_state=new_state,
                ):
                    break
                continue

            if self._is_known_page(new_activity, new_hierarchy, new_state):
                self.metrics.inc("known_states_skipped")
            else:
                self._try_screenshot(
                    new_activity, depth + 1, new_hierarchy, new_state
                )
                new_template = fingerprint.template_key(
                    new_hierarchy,
                    new_activity,
                )
                same_template_streak = 0
                for template in reversed(
                    path_templates + (parent_template,)
                ):
                    if template != new_template:
                        break
                    same_template_streak += 1

                if (
                    depth < MAX_DEPTH
                    and same_template_streak
                    < MAX_SAME_TEMPLATE_STREAK
                ):
                    self.template_expansions[new_template] += 1
                    self._explore_page(
                        depth=depth + 1,
                        initial_hierarchy=new_hierarchy,
                        path_templates=(
                            path_templates + (parent_template,)
                        ),
                    )
                else:
                    self.explored_state_keys.add(new_state)
                    if same_template_streak >= MAX_SAME_TEMPLATE_STREAK:
                        self.metrics.inc("same_template_streak_stops")
                    self.metrics.inc("depth_limit_hits")

            if not self._back_to_state(
                activity,
                parent_state,
                parent_template,
                current_state=new_state,
            ):
                self.metrics.inc("parent_restore_failures")
                break

    # ------------------------------------------------------------------
    # 判重与截图
    # ------------------------------------------------------------------

    def _is_known_page(self, activity, hierarchy=None, state=""):
        try:
            hierarchy = hierarchy or self._dump_hierarchy()
            if not hierarchy:
                return True

            if SKIP_PERSONAL_PAGES and not self._is_main_activity(activity):
                if privacy.is_personal_page(hierarchy, activity):
                    return True

            state = state or fingerprint.state_key(hierarchy, activity)
            return state in self.explored_state_keys
        except Exception as error:
            self.metrics.inc("known_page_errors")
            print(f"  [WARN] 状态判重失败: {error}")
            return True

    def _try_screenshot(self, activity, depth=0, hierarchy=None, state=""):
        try:
            hierarchy = hierarchy or self._dump_hierarchy()
            if not hierarchy:
                return False
            state = state or fingerprint.state_key(hierarchy, activity)
            if state in self.captured_state_keys:
                return False

            if SKIP_PERSONAL_PAGES and not self._is_main_activity(activity):
                if privacy.is_personal_page(hierarchy, activity):
                    self.metrics.inc("privacy_states_skipped")
                    return False

            fp = fingerprint.generate(hierarchy, activity)

            if SKIP_BLOCKED_POPUPS and popup_handler.has_blocking_popup(self.poco):
                self.metrics.inc("popup_states_skipped")
                return False

            self.metrics.inc("capture_attempts")
            with self.metrics.timed("capture"):
                path = screenshot.capture(
                    self.serial, activity, fp, segment_index=0
                )
            if path:
                phash = image_dedupe.compute_phash(path)
                if image_dedupe.find_similar(
                    phash, self.image_phashes, IMAGE_PHASH_THRESHOLD
                ):
                    os.remove(path)
                    self.captured_state_keys.add(state)
                    self.metrics.inc("image_duplicates_rejected")
                    return False

                image_sha256 = image_dedupe.sha256_file(path)
                record = metadata.build_record(
                    path, activity, fp, depth, self.device_info, self.app_info,
                    segment_index=0,
                    image_phash=phash,
                    image_sha256=image_sha256,
                )
                metadata.append_record(record)
                fingerprint.add_fingerprint(fp, self.visited_fingerprints)
                self.image_phashes.append(phash)
                self.captured_state_keys.add(state)
                self.screenshots_taken += 1
                self.metrics.inc("screenshots_saved")
                elapsed = time.monotonic() - self.run_started
                print(f"  [{self.screenshots_taken}] {activity.split('/')[-1]} ({elapsed:.0f}s)")
                return True
            self.metrics.inc("capture_quality_or_io_failures")
            return False
        except Exception as error:
            self.metrics.inc("capture_exceptions")
            print(f"  [WARN] 截图失败: {error}")
            return False

    # ------------------------------------------------------------------
    # 导航
    # ------------------------------------------------------------------

    def _get_current_activity(self):
        with self.metrics.timed("activity_query"):
            activity = metadata.get_current_activity(self.serial)
        self.metrics.inc("activity_queries")
        return activity

    def _read_current_state(self):
        activity = self._get_current_activity()
        hierarchy = self._dump_hierarchy()
        state = fingerprint.state_key(hierarchy, activity)
        return activity, hierarchy, state

    def _wait_for_stable_state(self, previous_state):
        """Activity 优先、控件树按需采样的分级稳定等待。"""
        started = time.monotonic()
        deadline = started + STATE_WAIT_TIMEOUT
        previous_activity = previous_state.split("|", 1)[0]
        last_activity = ""
        activity_stable_samples = 0
        last_state = ""
        stable_samples = 0
        observation = ("", None, "")

        while time.monotonic() < deadline:
            activity = self._get_current_activity()
            if activity and activity == last_activity:
                activity_stable_samples += 1
            else:
                last_activity = activity
                activity_stable_samples = 1

            if (
                activity
                and activity != previous_activity
                and activity_stable_samples >= STATE_STABLE_SAMPLES
            ):
                hierarchy = self._dump_hierarchy()
                state = fingerprint.state_key(hierarchy, activity)
                return activity, hierarchy, state

            elapsed = time.monotonic() - started
            if elapsed >= 0.35:
                hierarchy = self._dump_hierarchy()
                state = fingerprint.state_key(hierarchy, activity)
                observation = (activity, hierarchy, state)
                if state and state == last_state:
                    stable_samples += 1
                else:
                    last_state = state
                    stable_samples = 1

                if (
                    state
                    and state != previous_state
                    and stable_samples >= STATE_STABLE_SAMPLES
                ):
                    return observation

                if elapsed >= min(PAGE_LOAD_WAIT, 0.5):
                    return observation

            time.sleep(STATE_STABLE_INTERVAL)

        self.metrics.inc("state_wait_timeouts")
        return observation

    def _back_to_state(
        self,
        target_activity,
        target_state,
        target_template,
        press_back=True,
        current_state="",
    ):
        if not press_back:
            activity = self._get_current_activity()
            return activity == target_activity

        for _ in range(2):
            self._go_back()
            activity, hierarchy, observed_state = self._wait_for_stable_state(
                current_state or target_state
            )
            current_template = fingerprint.template_key(
                hierarchy,
                activity,
            )
            if activity == target_activity:
                if current_template != target_template:
                    self.metrics.inc(
                        "parent_template_mismatch_accepted"
                    )
                return True
            if not activity or PACKAGE_NAME not in activity:
                self._ensure_on_main_page()
                return False
            current_state = observed_state
            self.metrics.inc("parent_restore_retries")
        return False

    def _limit_actions_for_depth(self, actions, depth):
        if depth <= 0:
            return actions
        limit = (
            DEPTH_1_ACTION_LIMIT
            if depth == 1
            else DEPTH_2_ACTION_LIMIT
        )
        if len(actions) > limit:
            self.metrics.inc(
                "deep_actions_pruned",
                len(actions) - limit,
            )
        return actions[:limit]

    def _handle_left_app(self):
        try:
            time.sleep(1.5)
            now = metadata.get_current_activity(self.serial)
            if now and PACKAGE_NAME in now:
                return
            for _ in range(3):
                self._go_back()
                time.sleep(BACK_WAIT)
                now = metadata.get_current_activity(self.serial)
                if now and PACKAGE_NAME in now:
                    return
            self._restart_app()
        except Exception:
            self._restart_app()

    def _ensure_on_main_page(self):
        try:
            activity = metadata.get_current_activity(self.serial)
            if self._is_main_activity(activity):
                return
            print(f"  [DEBUG] 不在首页, 当前activity={activity}, 尝试返回")
            if activity and PACKAGE_NAME in activity:
                for _ in range(5):
                    self._go_back()
                    time.sleep(BACK_WAIT)
                    activity = metadata.get_current_activity(self.serial)
                    if self._is_main_activity(activity):
                        return
                    if not activity or PACKAGE_NAME not in activity:
                        break
        except Exception:
            pass
        print("  [DEBUG] 返回首页失败, 重启App")
        self._restart_app()
        time.sleep(1)

    def _is_main_activity(self, activity):
        return bool(activity) and MAIN_ACTIVITY_KEYWORD in activity and PACKAGE_NAME in activity

    # ------------------------------------------------------------------
    # Tab查找
    # ------------------------------------------------------------------

    def _count_valid_tabs(self):
        tabs = self._find_tabs()
        return sum(1 for t in tabs if not self._is_publish_button(t))

    def _find_tabs(self):
        patterns = ["main_tab", "tab_bar", "bottom_nav", "navigation_bar",
                    "BottomNavigationView", "RadioGroup"]
        for p in patterns:
            try:
                node = self.poco(nameMatches=f".*{p}.*")
                if node.exists():
                    children = list(node.children())
                    if len(children) >= 3:
                        return children
            except Exception:
                continue
        return self._find_bottom_nodes()

    def _find_bottom_nodes(self):
        try:
            nodes = self.poco(touchable=True)
            bottom = []
            for node in nodes:
                try:
                    pos = node.attr("pos")
                    if pos and pos[1] > 0.88:
                        bottom.append((pos[0], node))
                except Exception:
                    continue
            if len(bottom) < 3:
                return []
            bottom.sort(key=lambda t: t[0])
            return [t[1] for t in bottom]
        except Exception:
            return []

    def _is_publish_button(self, node):
        try:
            text = node.attr("text") or ""
            desc = node.attr("desc") or ""
            name = node.attr("name") or ""
            content = text + desc + name
            return any(kw in content for kw in
                       ["+", "发布", "拍摄", "publish", "create", "CenterPlus", "投稿", "post"])
        except Exception:
            return False

    # ------------------------------------------------------------------
    # 控件收集
    # ------------------------------------------------------------------

    def _get_actions(self, hierarchy=None):
        raw = []
        seen = set()

        hierarchy = hierarchy or self._dump_hierarchy()
        if not hierarchy:
            return []

        def _walk(node):
            payload = node.get("payload", {})
            if not payload.get("visible", True):
                return

            touchable = payload.get("touchable") or payload.get("clickable")
            if touchable and payload.get("enabled", True):
                text = payload.get("text") or ""
                desc = payload.get("desc") or payload.get("description") or ""
                ntype = payload.get("type") or ""
                name = payload.get("name") or ""
                pos = payload.get("pos")

                if (
                    pos
                    and len(pos) >= 2
                    and 0 <= pos[0] <= 1
                    and 0 <= pos[1] <= 1
                    and not any(kw in (text + desc) for kw in SKIP_TEXTS)
                    and not self._is_tab_node(name)
                ):
                    aid = self._action_id(ntype, name, text, pos)
                    if aid not in seen:
                        seen.add(aid)
                        raw.append({
                            "id": aid,
                            "priority": self._priority(ntype, name, text),
                            "x": pos[0],
                            "y": pos[1],
                        })

            for child in node.get("children", []):
                _walk(child)

        _walk(hierarchy)
        raw.sort(key=lambda a: a["priority"], reverse=True)

        # 卡片合并: 只对低优先级节点(列表项)做位置去重
        # 高优先级节点(导航/搜索/频道入口)不合并, 全部保留
        result = []
        used = []
        low_priority_count = 0
        for a in raw:
            if a["priority"] < 60:
                if low_priority_count >= LIST_ITEM_MAX_CLICK:
                    continue
                dup = False
                for ux, uy in used:
                    if abs(a["y"] - uy) < 0.02 and abs(a["x"] - ux) < 0.1:
                        dup = True
                        break
                if dup:
                    continue
                used.append((a["x"], a["y"]))
                low_priority_count += 1
            result.append(a)

        return result

    def _is_tab_node(self, name):
        if not name:
            return False
        return any(kw in name.lower() for kw in TAB_ID_KEYWORDS)

    def _click(self, action):
        try:
            x = int(action["x"] * self.screen_w)
            y = int(action["y"] * self.screen_h)
            result = subprocess.run(
                [ADB, "-s", self.serial, "shell", "input", "tap", str(x), str(y)],
                capture_output=True,
                timeout=5,
            )
            return result.returncode == 0
        except (KeyError, OSError, subprocess.SubprocessError):
            return False

    # ------------------------------------------------------------------
    # ADB
    # ------------------------------------------------------------------

    def _go_back(self):
        subprocess.run(
            [ADB, "-s", self.serial, "shell", "input", "keyevent", "4"],
            capture_output=True, timeout=5
        )

    def _restart_app(self):
        self.metrics.inc("app_restarts")
        subprocess.run(
            [ADB, "-s", self.serial, "shell", "am", "force-stop", PACKAGE_NAME],
            capture_output=True, timeout=5
        )
        time.sleep(1)
        subprocess.run(
            [ADB, "-s", self.serial, "shell", "monkey", "-p", PACKAGE_NAME,
             "-c", "android.intent.category.LAUNCHER", "1"],
            capture_output=True, timeout=5
        )
        time.sleep(3)
        popup_handler.dismiss_popups(self.poco, max_attempts=3)

    def _dump_hierarchy(self):
        try:
            with self.metrics.timed("hierarchy_dump"):
                hierarchy = self.poco.agent.hierarchy.dump()
            self.metrics.inc("hierarchy_dumps")
            return hierarchy
        except Exception as error:
            self.metrics.inc("hierarchy_dump_failures")
            print(f"  [WARN] hierarchy dump失败: {error}")
            return None

    def _swipe(self, y1, y2):
        cx = self.screen_w // 2
        try:
            subprocess.run(
                [ADB, "-s", self.serial, "shell", "input", "swipe",
                 str(cx), str(int(self.screen_h * y1)),
                 str(cx), str(int(self.screen_h * y2)), "400"],
                capture_output=True, timeout=5,
            )
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------

    def _action_id(self, ntype, name, text, pos):
        location = f"@{round(pos[0] * 20) / 20},{round(pos[1] * 20) / 20}"
        if name and name != "None":
            return f"{ntype}:{name}{location}"
        if text:
            return f"{ntype}:{text[:20]}{location}"
        return f"{ntype}:{location}"

    def _priority(self, ntype, name, text):
        score = 0
        if any(kw in name.lower() for kw in ["menu", "drawer", "more", "setting", "search"]):
            score += 100
        if any(kw in text for kw in ["设置", "更多", "全部", "频道", "搜索", "分类"]):
            score += 80
        if ntype in ["TextView", "Button", "ImageButton"]:
            score += 40
        return score
