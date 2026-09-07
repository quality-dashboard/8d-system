"""
config.py - 8D客诉整改跟踪系统 全局配置
------------------------------------------------
集中管理系统常量, 改字段/类型/状态/规则只改这里, 业务代码不再写死。
所有状态字符串统一用 STATUS_* 常量, 避免各处硬编码导致状态机不一致。
"""
from datetime import date

# ---------- 系统信息 ----------
SYSTEM_NAME = "8D客诉整改跟踪系统"
VERSION = "v0.4"

# ---------- 业务规则常量 ----------
ANALYSIS_DAYS = 7          # 分析截止 = 收货日期 + 7个自然日
REMIND_BEFORE_DAYS = 2     # 截止日期前2天内开始提醒(即将到期)
TRACK_MONTHS = 2           # 措施跟踪期: 连续2个月
FEEDBACK_ROUNDS = 2        # 一个跟踪期内需要提交几次反馈

# ---------- 问题类型(六选一) ----------
PROBLEM_TYPES = ["尺寸超差", "表面缺陷", "材料异常", "性能不达标", "外观不良", "其他"]

# ---------- 状态流转 ----------
# 待分析 → 跟踪中 → 待关闭 → 已关闭
#                 ↘ 待措施 ↗  (反馈不达标 或 经理退回 → 重新制定措施, 跟踪期重新开始)
STATUS_ANALYSIS = "待分析"
STATUS_ACTION = "待措施"
STATUS_TRACKING = "跟踪中"
STATUS_PENDING_CLOSE = "待关闭"
STATUS_CLOSED = "已关闭"
ALL_STATUS = [STATUS_ANALYSIS, STATUS_ACTION, STATUS_TRACKING, STATUS_PENDING_CLOSE, STATUS_CLOSED]

# 每个状态在页面上给操作人的一句话提示(统一来源, 页面只负责展示)
STATUS_TIPS = {
    STATUS_ANALYSIS: "工程师需在「分析截止日期」前完成根因分析并制定改善措施",
    STATUS_ACTION: "需重新制定改善措施，提交后跟踪期(2个月×2次反馈)重新开始计算",
    STATUS_TRACKING: "跟踪期内每月提交一次反馈，共2次；两次均达标后自动转「待关闭」；任一次反馈不合格立即转「待措施」",
    STATUS_PENDING_CLOSE: "两次反馈均达标，等待经理确认关闭；经理也可退回重新整改",
    STATUS_CLOSED: "客诉已闭环归档，可在「已关闭列表」查看整改进过",
}

# ---------- 角色定义 ----------
# 中文角色: 页面展示用(原有常量, 保持不变)
ROLE_ENGINEER = "工程师"
ROLE_MANAGER = "经理"
ROLES = [ROLE_ENGINEER, ROLE_MANAGER]

# 角色码: accounts 表的 role 字段存英文, 与中文角色双向映射
ROLE_CODE_ENGINEER = "engineer"
ROLE_CODE_MANAGER = "manager"
ROLE_CODES = [ROLE_CODE_ENGINEER, ROLE_CODE_MANAGER]
ROLE_CODE_TO_CN = {ROLE_CODE_ENGINEER: ROLE_ENGINEER, ROLE_CODE_MANAGER: ROLE_MANAGER}
ROLE_CN_TO_CODE = {ROLE_ENGINEER: ROLE_CODE_ENGINEER, ROLE_MANAGER: ROLE_CODE_MANAGER}

# 各角色可见的页面(权限的唯一出处, 页面渲染和接口校验都按它来)
# 说明: 经理只做把关, 不做登记/分析/反馈, 所以没有工程师的操作页
ROLE_PAGES = {
    ROLE_CODE_ENGINEER: ["新增客诉", "客诉列表", "待处理任务", "根因措施", "跟踪反馈"],
    ROLE_CODE_MANAGER: ["客诉列表", "待关闭确认", "已关闭列表", "分析看板"],
}

# ---------- 账号与登录 ----------
PASSWORD_MIN_LEN = 6          # 密码最短长度
PASSWORD_ITERATIONS = 260000  # PBKDF2迭代次数(越大越慢越安全, 单次校验约0.2秒)
SESSION_TIMEOUT_HOURS = 8     # 无操作超过N小时自动登出(0=不自动登出)
ALERT_STICKY = False          # 顶部提醒条是否吸顶(True=长列表滚动时始终可见)

# ---------- 是/否 选项 ----------
YES = "是"
NO = "否"
YES_NO = [YES, NO]

# ---------- 提醒配色(Streamlit markdown用) ----------
COLOR_OVERDUE = "red"      # 已逾期
COLOR_DUE_SOON = "orange"  # 即将到期(前2天)
COLOR_INFO = "blue"        # 普通提醒
COLOR_OK = "green"         # 正常/通过

# 顶部提醒条 / 图表的十六进制配色(HTML+CSS 与 Plotly 共用一套)
COLOR_HEX = {
    "overdue": "#d62728",   # 逾期: 红
    "soon": "#ff7f0e",      # 即将到期: 橙
    "pending": "#1f77b4",   # 待关闭/进行中: 蓝
    "ok": "#2ca02c",        # 正常/已关闭: 绿
    "gray": "#9e9e9e",      # 无任务/历史: 灰
}

# ---------- 看板 ----------
TREND_MONTHS = 12          # 关闭趋势默认展示最近12个月
SIMILAR_CHECK_MONTHS = 12  # 新增客诉时, 同类问题(同客户+同问题类型)的回溯月数

# ---------- 反馈提交时效文案 ----------
# 用于反馈列表的"提交时效"列, 与逾期提醒的红/绿配色保持一致
TIMELINESS_MARK = {
    "ok": "🟢",        # 按时提交
    "late": "🔴",      # 逾期提交
    "unknown": "⚪",   # 无法判断(历史轮次/缺日期)
}

# ---------- 调试模式 ----------
# 开启后允许手工指定"措施完成时间", 用于本地验证反馈逾期/即将到期提醒。
# 线上使用时保持 False(侧边栏开关默认关闭)。
DEBUG_MODE = False
