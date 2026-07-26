"""有界 MAX_DEPTH 遍历实验适配器。

原始实现仍来自 core.traversal.TraversalEngine。本文件不修改点击、等待、
截图、去重、回退或动作排序逻辑。实验只改变深度策略：提高 MAX_DEPTH 时，
对深层状态设置递减动作预算和同 Activity 展开预算，避免 DFS 长时间陷入
少数详情页分支。
"""

from collections import Counter
import time

from core import traversal as original_traversal


class DepthExperimentTraversalEngine(original_traversal.TraversalEngine):
    """复用原版遍历器，注入有界深度策略并记录空耗指标。"""

    def __init__(
        self,
        *args,
        experiment_max_depth: int,
        depth_action_limits: dict[int, int] | None = None,
        max_deep_activity_expansions: int = 2,
        **kwargs,
    ):
        if experiment_max_depth < 1:
            raise ValueError("experiment_max_depth 必须大于等于 1")
        if max_deep_activity_expansions < 1:
            raise ValueError("max_deep_activity_expansions 必须大于等于 1")
        self.experiment_max_depth = experiment_max_depth
        self.depth_action_limits = depth_action_limits or {1: 4, 2: 2}
        self.max_deep_activity_expansions = max_deep_activity_expansions
        self.explore_calls_by_depth = Counter()
        self.explore_inclusive_seconds_by_depth = Counter()
        self.actions_discovered_by_depth = Counter()
        self.actions_returned_by_depth = Counter()
        self.actions_pruned_by_depth = Counter()
        self.screenshots_saved_by_depth = Counter()
        self.deep_activity_expansions = Counter()
        self.deep_activity_expansion_skips = 0
        self.max_depth_invoked = 0
        self.backtrack_calls = 0
        self.backtrack_failures = 0
        self.backtrack_seconds = 0.0
        self.go_back_calls = 0
        self.go_back_seconds = 0.0
        self.restart_calls = 0
        self.restart_seconds = 0.0
        self._active_depth = 0
        self._active_activity = ""
        super().__init__(*args, **kwargs)

    def _explore_page(
        self, depth=0, parent_activities=None, path_states=None
    ):
        self.explore_calls_by_depth[depth] += 1
        self.max_depth_invoked = max(self.max_depth_invoked, depth)

        previous_max_depth = original_traversal.MAX_DEPTH
        previous_active_depth = self._active_depth
        original_traversal.MAX_DEPTH = self.experiment_max_depth
        self._active_depth = depth
        started = time.monotonic()
        try:
            return super()._explore_page(
                depth=depth,
                parent_activities=parent_activities,
                path_states=path_states,
            )
        finally:
            self.explore_inclusive_seconds_by_depth[depth] += (
                time.monotonic() - started
            )
            self._active_depth = previous_active_depth
            original_traversal.MAX_DEPTH = previous_max_depth

    def _try_screenshot(self, activity):
        self._active_activity = activity
        saved = super()._try_screenshot(activity)
        if saved:
            self.screenshots_saved_by_depth[self._active_depth] += 1
        return saved

    def _get_actions(self):
        actions = super()._get_actions()
        depth = self._active_depth
        self.actions_discovered_by_depth[depth] += len(actions)

        # Depth=1 对照组不会在深层调用 _get_actions，因此预算只影响更深实验组。
        if self.experiment_max_depth > 1 and depth > 0:
            activity = self._active_activity
            if (
                self.deep_activity_expansions[activity]
                >= self.max_deep_activity_expansions
            ):
                self.actions_pruned_by_depth[depth] += len(actions)
                self.deep_activity_expansion_skips += 1
                return []
            self.deep_activity_expansions[activity] += 1

            limit = self.depth_action_limits.get(depth)
            if limit is not None:
                limited = actions[:limit]
                self.actions_pruned_by_depth[depth] += (
                    len(actions) - len(limited)
                )
                actions = limited

        self.actions_returned_by_depth[depth] += len(actions)
        return actions

    def _back_to(self, target):
        self.backtrack_calls += 1
        started = time.monotonic()
        try:
            success = super()._back_to(target)
            if not success:
                self.backtrack_failures += 1
            return success
        finally:
            self.backtrack_seconds += time.monotonic() - started

    def _go_back(self):
        self.go_back_calls += 1
        started = time.monotonic()
        try:
            return super()._go_back()
        finally:
            self.go_back_seconds += time.monotonic() - started

    def _restart_app(self):
        self.restart_calls += 1
        started = time.monotonic()
        try:
            return super()._restart_app()
        finally:
            self.restart_seconds += time.monotonic() - started

    @staticmethod
    def _counter_dict(counter):
        return {
            str(key): round(value, 6)
            for key, value in sorted(counter.items())
        }

    def experiment_metrics(self) -> dict:
        return {
            "configured_max_depth": self.experiment_max_depth,
            "depth_action_limits": {
                str(depth): limit
                for depth, limit in sorted(self.depth_action_limits.items())
            },
            "max_deep_activity_expansions": (
                self.max_deep_activity_expansions
            ),
            "max_depth_invoked": self.max_depth_invoked,
            "explore_calls_by_depth": self._counter_dict(
                self.explore_calls_by_depth
            ),
            "explore_inclusive_seconds_by_depth": self._counter_dict(
                self.explore_inclusive_seconds_by_depth
            ),
            "actions_discovered_by_depth": self._counter_dict(
                self.actions_discovered_by_depth
            ),
            "actions_returned_by_depth": self._counter_dict(
                self.actions_returned_by_depth
            ),
            "actions_pruned_by_depth": self._counter_dict(
                self.actions_pruned_by_depth
            ),
            "deep_actions_pruned": sum(
                count
                for depth, count in self.actions_pruned_by_depth.items()
                if depth > 0
            ),
            "screenshots_saved_by_depth": self._counter_dict(
                self.screenshots_saved_by_depth
            ),
            "deep_activity_expansion_skips": (
                self.deep_activity_expansion_skips
            ),
            "backtrack_calls": self.backtrack_calls,
            "backtrack_failures": self.backtrack_failures,
            "backtrack_seconds": self.backtrack_seconds,
            "go_back_calls": self.go_back_calls,
            "go_back_seconds": self.go_back_seconds,
            "restart_calls": self.restart_calls,
            "restart_seconds": self.restart_seconds,
            "screenshots": self.screenshots_taken,
            "actions": self.total_actions,
        }
