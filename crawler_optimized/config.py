"""哔哩哔哩配置"""

import os

# App 配置
PACKAGE_NAME = "tv.danmaku.bili"
MAIN_ACTIVITY_KEYWORD = "MainActivityV2"  # 判断是否在首页的关键词

# 连接方式: "usb" / "wifi" / "emulator"
CONNECTION_MODE = "usb"

# WiFi ADB 配置（CONNECTION_MODE="wifi" 时生效）
WIFI_ADB_HOST = "192.168.43.1"
WIFI_ADB_PORT = 5555

# 模拟器配置（CONNECTION_MODE="emulator" 时生效）
EMULATOR_HOST = "127.0.0.1"
EMULATOR_PORT = 5555  # MuMu 开启了默认ADB端口

# 模拟器实测中深层回退成本高于新增有效图收益；默认先完成横向覆盖。
MAX_DEPTH = 1
LIST_ITEM_MAX_CLICK = 2
# 深层状态只抽样高价值动作，避免 DFS 在单个详情链耗尽预算。
DEPTH_1_ACTION_LIMIT = 2
DEPTH_2_ACTION_LIMIT = 1
MAX_SAME_TEMPLATE_STREAK = 1
MAX_SCREENSHOTS = 200
MAX_STATES = 500
MAX_TOTAL_ACTIONS = 500
MAX_RUNTIME_SECONDS = 1200
SCROLL_MAX_TIMES = 3
PAGE_LOAD_WAIT = 0.8
BACK_WAIT = 0.5
STATE_WAIT_TIMEOUT = 3.0
STATE_STABLE_INTERVAL = 0.15
STATE_STABLE_SAMPLES = 2

# 截图近重复判定：64-bit pHash 汉明距离小于等于该值视为近重复
IMAGE_PHASH_THRESHOLD = 6

# 弹窗遮挡判定：顶层节点可见面积 >= 此比例视为遮挡弹窗
POPUP_OCCLUSION_RATIO = 0.5

# 隐私页跳过（个人详情页不截图）
SKIP_PERSONAL_PAGES = True
# 遮挡弹窗跳过截图 —— 先关闭以保证遍历覆盖（弹窗仍会尝试关闭，只是不因此跳过截图）
SKIP_BLOCKED_POPUPS = False
PRIVACY_ACTIVITY_KEYWORDS = ["MineActivity", "UserSpaceActivity", "PersonalInfoActivity", "MemberCenter"]
PRIVACY_ID_KEYWORDS = ["mine_header", "personal_info", "user_card", "member_center"]
PRIVACY_TEXT_KEYWORDS = ["编辑资料", "我的钱包", "我的订单", "离线缓存", "稍后再看", "历史记录"]
PRIVACY_TEXT_MIN_HITS = 2  # 至少命中2个才判定为个人页

# 不可点击的控件文本（危险操作或无效操作）
SKIP_TEXTS = [
    "发布", "拍摄", "直播", "充值", "开通",
    "分享", "收藏", "转发", "投币", "投稿",
    "删除", "举报", "拉黑", "+",
]

# 需要跳过的Activity关键词（进入这些页面不截图不递归）
SKIP_ACTIVITY_KEYWORDS = ["Publish", "Upload", "Editor", "Camera", "Shoot", "Record", "Live", "ImageCrop", "DanmakuCompose"]

# 弹窗关闭关键词（含冷启动引导：跳过广告/同意隐私协议/青少年模式）
POPUP_CLOSE_KEYWORDS = [
    "我知道了", "取消", "跳过", "以后再说",
    "暂不", "关闭", "不再提示", "稍后",
    "允许", "下次再说", "暂不更新",
    "skip", "Skip", "同意", "同意并继续", "知道了", "继续",
    "关闭广告", "青少年", "以后再说", "暂不升级",
    "不感兴趣",
]

# 输出目录
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(BASE_DIR, "output")
SCREENSHOT_DIR = os.path.join(OUTPUT_DIR, "screenshots")
METADATA_CSV = os.path.join(OUTPUT_DIR, "metadata.csv")
STATE_FILE = os.path.join(OUTPUT_DIR, "state.json")
RUN_METRICS_FILE = os.path.join(OUTPUT_DIR, "run_metrics.json")

# Metadata 默认值，可由批处理平台通过环境变量注入
SOURCE_BATCH = os.environ.get("CRAWLER_SOURCE_BATCH", "")
DEFAULT_LANGUAGE = os.environ.get("CRAWLER_LANGUAGE", "")
DEFAULT_CATEGORY = os.environ.get("CRAWLER_CATEGORY", "")
DEFAULT_DESIGN_STYLE = os.environ.get("CRAWLER_DESIGN_STYLE", "")

os.makedirs(SCREENSHOT_DIR, exist_ok=True)
