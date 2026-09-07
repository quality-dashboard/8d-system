"""
app.py - 8D客诉整改跟踪系统 主程序 (v0.3)
------------------------------------------------
流程(简道云闭环 + MantisBT状态通知思想):
  录入客诉(自动算分析截止) → 工程师填根因+措施(自动算反馈截止)
  → 跟踪中每月反馈一次 ×2 → 两次达标自动"待关闭" → 经理确认"已关闭"
  反馈未达标 → 自动转"待措施" → 工程师重填措施 → 跟踪期重新开始
  经理发现异常可退回"待措施"重新整改, 跟踪期同样重新开始。

页面(角色由登录账号决定, 不能手动切换):
  工程师: 新增客诉 / 客诉列表(仅自己) / 待处理任务 / 根因措施 / 跟踪反馈
  经理  : 客诉列表(全部) / 待关闭确认 / 已关闭列表 / 分析看板

v0.2 改动:
  1) 顶部提醒条: 逾期数量红色标记
  2) 新增工程师「待处理任务」页
  3) 新增经理「已关闭列表」页
  4) 分析看板新增「关闭趋势」
  5) 性能优化: 全表一次查询 + 内存过滤; 读操作加ttl缓存
  6) 调试模式

v0.3 改动(本次):
  1) 账号登录: accounts表 + PBKDF2密码哈希 + 未登录只显示登录页
  2) 角色由账号决定, 侧边栏角色下拉框已移除
  3) 数据隔离: 工程师只能看自己负责的客诉(SQL层过滤, 不是前端隐藏)
  4) 权限三层防护: 查询层过滤 / 写操作归属校验 / 经理操作角色校验
  5) 首次登录强制改密码后才能使用系统
  6) 提醒区按角色区分, 经理多一张"待关闭"卡片
  7) 逾期任务在列表中置顶并红色标记

v0.4 改动(本次):
  1) 反馈判定规则修订: 第1次反馈就不合格(措施有效=否 或 同类投诉=是)时,
     立即转"待措施"并作废本轮反馈(置负留痕), 不再等第2次反馈
  2) 第1次反馈合格、第2次不合格, 仍按原规则在整轮判定后转"待措施"
  3) 跟踪反馈提交提示改为"存session_state后rerun再显示",
     修复提示被st.rerun()吞掉看不到的问题
"""
from datetime import date, datetime, timedelta

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import db
from config import (
    SYSTEM_NAME, VERSION, PROBLEM_TYPES, ALL_STATUS,
    ROLE_CODE_MANAGER,
    PASSWORD_MIN_LEN, SESSION_TIMEOUT_HOURS, ALERT_STICKY,
    ANALYSIS_DAYS, REMIND_BEFORE_DAYS, TRACK_MONTHS, FEEDBACK_ROUNDS,
    TREND_MONTHS, SIMILAR_CHECK_MONTHS, DEBUG_MODE, STATUS_TIPS, COLOR_HEX,
    TIMELINESS_MARK,
    STATUS_ANALYSIS, STATUS_ACTION, STATUS_TRACKING,
    STATUS_PENDING_CLOSE, STATUS_CLOSED,
)

st.set_page_config(page_title=SYSTEM_NAME, page_icon="📋", layout="wide")


# ============================================================
# 一、初始化
# ============================================================

@st.cache_resource
def _init():
    try:
        db.init_db()
    except Exception as e:
        # 建表失败(凭证错误/网络问题)不白屏, 把真实原因显示出来
        st.error("⚠️ 数据库初始化失败: {}".format(e))
        st.stop()
    return True


def _need_config():
    """数据库凭证未配置时给出友好提示, 不让页面白屏报错"""
    if not db.TURSO_URL or not db.TURSO_AUTH_TOKEN:
        st.error(
            "⚠️ 数据库未配置。请在 .streamlit/secrets.toml 中填写:\n\n"
            "```\nTURSO_8D_URL = \"libsql://你的库名.turso.io\"\n"
            "TURSO_8D_TOKEN = \"你的turso-auth-token\"\n```\n\n"
            "(去 turso.io 控制台创建数据库, 复制URL和Token)"
        )
        st.stop()


def _clear_caches():
    """数据变更后清缓存, 保证列表/提醒区立刻刷新(实际清缓存动作在db层)"""
    db._invalidate()


# ============================================================
# 一之二、会话与登录(本次新增)
# ============================================================

# 会话里与登录相关的键, 退出登录时统一清空
SESSION_KEYS = ("user", "last_active", "must_change_pwd", "pwd_page")


def _current_user():
    """当前登录用户(dict)。未登录返回 None"""
    return st.session_state.get("user")


def _touch():
    """刷新最后活跃时间(用于会话超时判断)"""
    st.session_state["last_active"] = datetime.now()


def _session_expired():
    """无操作超过 SESSION_TIMEOUT_HOURS 小时判定超时(配成0则永不超时)"""
    if not SESSION_TIMEOUT_HOURS:
        return False
    last = st.session_state.get("last_active")
    if not last:
        return False
    return (datetime.now() - last) > timedelta(hours=SESSION_TIMEOUT_HOURS)


def _logout():
    """退出登录: 清空会话里的用户态, 回到登录页"""
    for k in SESSION_KEYS:
        st.session_state.pop(k, None)


def render_login_page():
    """登录页: 未登录时唯一可见的内容(用 Streamlit 原生 form 实现)"""
    st.title("📋 {}".format(SYSTEM_NAME))
    st.caption("客户投诉 → 根因分析 → 措施跟踪({}个月×{}次反馈) → 经理关闭".format(
        TRACK_MONTHS, FEEDBACK_ROUNDS))

    left, center, right = st.columns([1, 1.1, 1])
    with center:
        st.markdown("### 🔐 请登录")
        with st.form("login_form"):
            username = st.text_input("登录账号")
            password = st.text_input("密码", type="password")
            submitted = st.form_submit_button("登 录", type="primary",
                                              use_container_width=True)

        if submitted:
            if not username or not password:
                st.warning("请输入账号和密码")
            else:
                try:
                    ok, user, _reason = db.authenticate(username.strip(), password)
                except Exception as e:
                    st.error("⚠️ 登录失败: {}".format(e))
                else:
                    if not ok:
                        # 统一文案: 不区分"账号不存在/密码错/已停用", 防止被拿来枚举账号
                        st.error("账号或密码错误")
                    else:
                        # 登录成功: 写会话(账号dict里已剔除密码哈希)
                        st.session_state["user"] = user
                        # 首次登录(从未登录过)强制改密码后才能用
                        st.session_state["must_change_pwd"] = bool(
                            user.get("is_first_login"))
                        _touch()
                        st.rerun()

        st.caption("账号由质量部统一分配，忘记密码请联系经理重置。")


def render_change_password(force=False):
    """修改密码。
    force=True : 首次登录强制模式, 改完之前不给用系统(页面在改密处 st.stop())
    force=False: 侧边栏主动进入, 可返回系统"""
    user = _current_user()
    st.title("🔑 {}".format("首次登录，请先修改密码" if force else "修改密码"))
    if force:
        st.warning("这是您第一次登录系统，请先设置新密码。\n\n"
                   "密码要求：至少 **{}** 位，且必须同时包含 **字母和数字**。".format(
                       PASSWORD_MIN_LEN))
    else:
        st.info("密码要求：至少 **{}** 位，且必须同时包含 **字母和数字**。".format(
            PASSWORD_MIN_LEN))

    left, center, right = st.columns([1, 1.1, 1])
    with center:
        with st.form("change_pwd_form"):
            old_pwd = st.text_input("当前密码", type="password")
            new_pwd1 = st.text_input("新密码", type="password")
            new_pwd2 = st.text_input("确认新密码", type="password")
            submitted = st.form_submit_button("确认修改", type="primary",
                                              use_container_width=True)

        if submitted:
            if not old_pwd or not new_pwd1 or not new_pwd2:
                st.warning("请把三项都填写完整")
            elif new_pwd1 != new_pwd2:
                st.error("两次输入的新密码不一致")
            else:
                # 先验证当前密码, 防止会话被他人接管后直接改密
                ok, _fresh, _r = db.authenticate(user.get("username"), old_pwd)
                if not ok:
                    st.error("当前密码不正确")
                else:
                    try:
                        db.update_password(user["id"], new_pwd1,
                                           user.get("username"))
                    except ValueError as e:
                        st.error("{}".format(e))
                    else:
                        st.success("✅ 密码修改成功")
                        st.session_state["must_change_pwd"] = False
                        st.session_state["pwd_page"] = False
                        st.rerun()

        if not force:
            if st.button("← 返回系统", use_container_width=True,
                         key="btn_back_sys"):
                st.session_state["pwd_page"] = False
                st.rerun()


# ---- 启动检查 ----
_need_config()   # 先检查凭证, 没配置就不连库
_init()          # 再建表 / 建账号表 / 补 owner_id 列(失败会显示具体原因)

# ---- 登录闸门: 未登录只渲染登录页, 后面所有业务代码都不会执行 ----
if not _current_user():
    render_login_page()
    st.stop()

# ---- 会话超时: 超时自动登出 ----
if _session_expired():
    _logout()
    st.warning("登录已超时，请重新登录")
    st.rerun()
_touch()

# ---- 当前用户与角色(角色由账号决定, 不提供手动切换) ----
user = _current_user()
role_code = db.role_code_of(user)
role_cn = db.role_cn_of(user)
is_manager = (role_code == ROLE_CODE_MANAGER)

# ---- 首次登录: 强制改密码后才能使用 ----
if st.session_state.get("must_change_pwd"):
    render_change_password(force=True)
    st.stop()

# ---- 主动进入的改密码页 ----
if st.session_state.get("pwd_page"):
    render_change_password(force=False)
    st.stop()

# 提醒数据和反馈索引整页只取一次, 各页面共用, 避免重复查库
# 注意: get_alerts 传了 viewer, 工程师只会统计到自己负责的客诉
try:
    alerts = db.get_alerts(viewer=user)
    fb_map = db.get_all_feedbacks()
    task_total = len(db.build_task_list(alerts, fb_map, viewer=user))
except Exception as e:
    # 网络抖动/凭证失效时不白屏, 明确告诉用户原因
    st.error("⚠️ 读取提醒数据失败: {}".format(e))
    st.stop()


# ============================================================
# 二、顶部提醒条(规则五.4: 顶部显示提醒数量, 逾期用红色标记)
# ============================================================

def _alert_cards(cards, sticky=False):
    """把提醒数量渲染成一排卡片(纯HTML+CSS, 轻量)。
    cards : [(标签, 数量, 主题色hex), ...]
    sticky: True 时提醒条吸顶(长列表滚动时始终可见), 由 config.ALERT_STICKY 控制
    数量为0时数字置灰, 有数量时数字用主题色(逾期=红色)高亮。"""
    wrap = "display:flex;gap:10px;flex-wrap:wrap;margin:2px 0 8px;"
    if sticky:
        wrap += ("position:sticky;top:0;z-index:990;background:#ffffff;"
                 "padding:6px 0 8px;")
    html = ['<div style="{}">'.format(wrap)]
    for label, count, color in cards:
        num_color = color if count else COLOR_HEX["gray"]
        html.append(
            '<div style="flex:1 1 140px;background:#ffffff;border:1px solid #e6e6e6;'
            'border-left:5px solid {c};border-radius:6px;padding:6px 14px;">'
            '<div style="font-size:12px;color:#888888;">{label}</div>'
            '<div style="font-size:26px;font-weight:700;color:{nc};'
            'line-height:1.25;">{n}</div>'
            '</div>'.format(c=color, nc=num_color, label=label, n=count))
    html.append("</div>")
    return "".join(html)


def render_top_alerts(alerts_data, manager_role, total_tasks):
    """页面顶部提醒条(固定在页面最上方, 无需滚动即可看到)。
    卡片: 待处理任务总数 → 分析逾期(红) → 反馈逾期(红) → 2天内到期(橙)
          → 待关闭(蓝, 仅经理)"""
    analysis_overdue = len(alerts_data["analysis_overdue"])
    feedback_overdue = (len(alerts_data["fb1_overdue"])
                        + len(alerts_data["fb2_overdue"]))
    overdue = analysis_overdue + feedback_overdue
    soon = sum(len(alerts_data[k]) for k in
               ("analysis_soon", "fb1_soon", "fb2_soon"))
    pending = len(alerts_data.get("pending_close", []))
    need_action = len(alerts_data.get("need_action", []))

    # 工程师看"我"的, 经理看全部门的
    cards = [
        ("待处理任务(全部)" if manager_role else "我的待处理任务",
         total_tasks, COLOR_HEX["pending"]),
        ("分析逾期", analysis_overdue, COLOR_HEX["overdue"]),
        ("反馈逾期", feedback_overdue, COLOR_HEX["overdue"]),
        ("{}天内到期".format(REMIND_BEFORE_DAYS), soon, COLOR_HEX["soon"]),
    ]
    if manager_role:
        cards.append(("待关闭", pending, COLOR_HEX["pending"]))

    st.markdown(_alert_cards(cards, sticky=ALERT_STICKY), unsafe_allow_html=True)

    # 口径说明: 说清"待处理任务"与后面几张卡是总览与细分的关系, 不是并列相加
    others = total_tasks - overdue - soon - need_action
    st.caption(
        "口径：待处理任务共 **{}** 条 ＝ 逾期 **{}**（分析 {} ＋ 反馈 {}）· "
        "{}天内到期 **{}** · 待重新制定措施 **{}** · 其他 **{}**".format(
            total_tasks, overdue, analysis_overdue, feedback_overdue,
            REMIND_BEFORE_DAYS, soon, need_action, max(others, 0)))

    if overdue:
        st.error("有 **{}** 条任务已逾期，请优先处理（逾期任务已在列表中置顶"
                 "并标红）".format(overdue))


# ============================================================
# 三、侧边栏(角色选择 + 调试模式 + 提醒明细)
# ============================================================

def _fmt_alert(items, label):
    """提醒区一行文案: 数量 + 客诉编号"""
    if not items:
        return None
    nos = ", ".join(c["complaint_no"] for c in items)
    return "{}: **{}** ({})".format(label, len(items), nos)


def render_sidebar(alerts_data, user, manager_role):
    """侧边栏: 当前用户 + 退出登录/改密码 + 调试模式 + 系统提醒明细。
    注意: 这里【没有】角色切换下拉框 —— 角色由登录账号决定, 不能手动切换。"""
    st.sidebar.title("📋 {}".format(SYSTEM_NAME))
    st.sidebar.caption("版本 {} · 今日 {}".format(VERSION, date.today().isoformat()))

    # 当前登录用户(角色来自账号, 只读展示)
    st.sidebar.markdown("👤 **{}**（{}）".format(
        user.get("display_name") or user.get("username"), role_cn))
    btn1, btn2 = st.sidebar.columns(2)
    with btn1:
        if st.button("🔑 改密码", use_container_width=True, key="btn_pwd"):
            st.session_state["pwd_page"] = True
            st.rerun()
    with btn2:
        if st.button("🚪 退出登录", use_container_width=True, key="btn_logout"):
            _logout()
            st.rerun()

    # 调试模式: 默认跟随config.DEBUG_MODE, 可在侧边栏临时打开(不落库, 刷新即恢复默认)
    debug = st.sidebar.checkbox("🛠️ 调试模式(可指定措施完成时间)", value=DEBUG_MODE)
    if debug:
        st.sidebar.caption("调试模式下填写根因措施时可手工指定措施完成时间，"
                           "用于验证反馈截止与逾期提醒，正式使用请关闭")

    st.sidebar.divider()
    st.sidebar.subheader("🔔 系统提醒")

    # 逾期(红) → 即将到期(橙) → 待关闭/待措施(蓝) 逐级展示
    red_items = [
        _fmt_alert(alerts_data["analysis_overdue"], "分析已逾期"),
        _fmt_alert(alerts_data["fb1_overdue"], "第1次反馈已逾期"),
        _fmt_alert(alerts_data["fb2_overdue"], "第2次反馈已逾期"),
    ]
    orange_items = [
        _fmt_alert(alerts_data["analysis_soon"], "分析即将到期"),
        _fmt_alert(alerts_data["fb1_soon"], "第1次反馈即将到期"),
        _fmt_alert(alerts_data["fb2_soon"], "第2次反馈即将到期"),
    ]
    has_red = any(red_items)
    has_orange = any(orange_items)

    for msg in red_items:
        if msg:
            st.sidebar.markdown(":red[● {}]".format(msg))
    for msg in orange_items:
        if msg:
            st.sidebar.markdown(":orange[● {}]".format(msg))
    # 待关闭只有经理需要关心(工程师侧不显示这条)
    if manager_role and alerts_data["pending_close"]:
        st.sidebar.markdown(":blue[● 待经理确认关闭: **{}** 条]".format(
            len(alerts_data["pending_close"])))
    if alerts_data["need_action"]:
        st.sidebar.markdown(":blue[● 待重新制定措施: **{}** 条]".format(
            len(alerts_data["need_action"])))
    if not (has_red or has_orange
            or (manager_role and alerts_data["pending_close"])
            or alerts_data["need_action"]):
        st.sidebar.success("✅ 暂无逾期和到期提醒")

    st.sidebar.divider()
    return debug


debug_mode = render_sidebar(alerts, user, is_manager)

st.title("📋 {}".format(SYSTEM_NAME))
st.caption("客户投诉 → 根因分析 → 措施跟踪({}个月×{}次反馈) → 经理关闭".format(
    TRACK_MONTHS, FEEDBACK_ROUNDS))
render_top_alerts(alerts, is_manager, task_total)

# ---- 操作结果闪现提示(v0.4): 提交反馈时提示先存session_state再rerun,
#      这里统一取出显示, 保证提示不会因为st.rerun()而丢失 ----
_flash = st.session_state.pop("fb_flash", None)
if _flash:
    _kind, _text = _flash
    if _kind == "error":
        st.error(_text)
    else:
        st.success(_text)


# ============================================================
# 四、通用工具函数
# ============================================================

# 截止日期等级 → 页面文案前缀(红/橙/绿)
_LEVEL_MARK = {
    "overdue": "🔴 已逾期",
    "soon": "🟠 即将到期",
    "normal": "🟢 正常",
    "none": "",
}


def _next_due(complaint, fb_map_data):
    """该客诉当前最该关注的截止日期(没有则返回None)。
    待分析 → 分析截止; 跟踪中 → 按已交反馈数取第1次或第2次反馈截止。"""
    status = complaint.get("status")
    if status == STATUS_ANALYSIS:
        return complaint.get("analysis_deadline")
    if status == STATUS_TRACKING:
        done = len(db.round_feedbacks(complaint, fb_map_data))
        if done == 0:
            return complaint.get("feedback1_due")
        if done < FEEDBACK_ROUNDS:
            return complaint.get("feedback2_due")
    return None


def _urgency_key(complaint, fb_map_data):
    """列表排序键: 最紧急的排最前。
    规则: 有下一步截止日期的排前面(截止日期越早越靠前 → 逾期的自然置顶),
          已关闭等没有下一步动作的排最后; 同日期按id升序, 保证顺序稳定。"""
    due = _next_due(complaint, fb_map_data)
    cid = complaint.get("id") or 0
    if not due:
        return (1, "9999-12-31", cid)
    return (0, str(due)[:10], cid)


def _due_text(due_str):
    """把一个截止日期渲染成带颜色的文案, 例如 '🔴 已逾期 3 天 (2026-08-01)'"""
    level = db.classify_deadline(due_str)
    if level == "none":
        return "-"
    days = (date.fromisoformat(str(due_str)[:10]) - date.today()).days
    if level == "overdue":
        return "🔴 已逾期 {} 天 ({})".format(-days, due_str)
    if level == "soon":
        return "🟠 {} 天后到期 ({})".format(days, due_str)
    return "🟢 {} 天后到期 ({})".format(days, due_str)


def _status_badge(complaint, fb_map_data):
    """客诉行的截止日期着色: 红=逾期 橙=即将到期"""
    status = complaint.get("status")
    if status == STATUS_ANALYSIS or status == STATUS_TRACKING:
        due = _next_due(complaint, fb_map_data)
        if due:
            return _due_text(due)
    if status == STATUS_ACTION:
        return ":orange[需重新制定措施，提交后跟踪期重新开始]"
    if status == STATUS_PENDING_CLOSE:
        return ":blue[等待经理确认关闭]"
    if status == STATUS_CLOSED:
        return ":green[已关闭 {}]".format(str(complaint.get("closed_at") or "")[:10])
    return ""


def _progress_bar(status):
    """生成横向五步进度条HTML(纯CSS, 轻量不影响加载):
    待分析 → 待措施 → 跟踪中 → 待关闭 → 已关闭
    已完成步骤=绿色(带✓), 当前步骤=蓝色(高亮), 未完成=灰色"""
    steps = ALL_STATUS   # 顺序即流程顺序
    if status not in steps:
        status = steps[0]
    cur = steps.index(status)
    GREEN, BLUE, GRAY = "#2ca02c", "#1f77b4", "#c8c8c8"

    parts = ['<div style="display:flex;align-items:flex-start;margin:6px 0 4px;">']
    for i, s in enumerate(steps):
        if i < cur:
            # 已完成: 绿色圆圈打勾
            bg, mark = GREEN, "&#10003;"
        elif i == cur:
            # 当前: 蓝色圆圈显示序号
            bg, mark = BLUE, str(i + 1)
        else:
            # 未完成: 灰色圆圈显示序号
            bg, mark = GRAY, str(i + 1)
        # 文字颜色: 已走到的步骤深色, 未到的浅灰
        text_color = "#333333" if i <= cur else "#999999"
        # 加粗当前步骤文字
        font_weight = "bold" if i == cur else "normal"
        parts.append(
            '<div style="display:flex;flex-direction:column;align-items:center;">'
            '<div style="width:24px;height:24px;border-radius:50%;background:{bg};'
            'color:#ffffff;font-size:12px;font-weight:bold;display:flex;'
            'align-items:center;justify-content:center;">{mark}</div>'
            '<span style="font-size:12px;margin-top:3px;color:{tc};font-weight:{fw};">{s}</span>'
            '</div>'.format(bg=bg, mark=mark, tc=text_color, fw=font_weight, s=s))
        # 节点间连接线: 已走过=绿色, 未走=浅灰(最后一个节点后不画)
        if i < len(steps) - 1:
            line_color = GREEN if i < cur else "#e0e0e0"
            parts.append(
                '<div style="flex:1;height:3px;background:{lc};'
                'margin:11px 4px 0 4px;min-width:30px;"></div>'.format(lc=line_color))
    parts.append("</div>")
    return "".join(parts)


def _feedback_table(feedbacks, complaint=None):
    """把反馈记录渲染成表格。
    含两列新增信息:
      - 应截止: 该次反馈对应的截止日期
      - 提交时效: 按时提交(绿) / 逾期X天提交(红) / 历史轮次(灰)
    complaint 为空时(理论上不会)只做基础展示, 不判时效。"""
    if not feedbacks:
        return None
    rows = []
    for fb in feedbacks:
        month = abs(fb.get("feedback_month") or 0)
        row = {
            "第几次": month,
            "反馈日期": fb.get("feedback_date") or "-",
            "措施有效?": fb.get("is_effective") or "-",
            "同类投诉?": fb.get("has_similar_complaint") or "-",
            "说明": fb.get("feedback_comment") or "",
        }
        if complaint:
            # 该次反馈对应的截止日期(第1次取feedback1_due, 第2次取feedback2_due)
            due = (complaint.get("feedback1_due") if month == 1
                   else complaint.get("feedback2_due"))
            text, level = db.feedback_timeliness(fb, complaint)
            row["应截止"] = due or "-"
            row["提交时效"] = "{} {}".format(TIMELINESS_MARK.get(level, ""), text)
        row["备注"] = "上一轮·已作废" if fb.get("is_void") else ""
        rows.append(row)
    return pd.DataFrame(rows)


def render_detail(complaint, fb_map_data):
    """客诉详情区块(列表页/已关闭列表页复用): 进度条 + 基本信息 + 整改历史"""
    st.markdown(_progress_bar(complaint["status"]), unsafe_allow_html=True)

    badge = _status_badge(complaint, fb_map_data)
    if badge:
        st.markdown(badge)

    # 状态提示: 告诉操作人"现在该干什么"
    tip = STATUS_TIPS.get(complaint.get("status"))
    if tip:
        st.caption("💡 {}".format(tip))

    st.markdown(
        "- 客诉编号: **{}**\n"
        "- 客户 / 型号 / 问题类型: {} / {} / {}\n"
        "- 收货日期: {} ｜ 分析截止: {}\n"
        "- 问题描述: {}".format(
            complaint["complaint_no"], complaint["customer_name"],
            complaint.get("product_model") or "-",
            complaint.get("problem_type") or "-",
            complaint.get("received_date") or "-",
            complaint.get("analysis_deadline") or "-",
            complaint.get("problem_description") or "-"))

    if complaint.get("root_cause"):
        st.markdown(
            "- 根因结论: {}\n"
            "- 改善措施: {}（措施日期: {}，反馈1截止: {}，反馈2截止: {}）".format(
                complaint["root_cause"], complaint.get("action_description") or "-",
                complaint.get("action_date") or "-",
                complaint.get("feedback1_due") or "-",
                complaint.get("feedback2_due") or "-"))

    if complaint.get("closed_at"):
        st.markdown("- 关闭时间: **{}**".format(complaint["closed_at"]))

    # 整改历史: 含所有轮次的反馈(作废的也展示, 便于追溯)
    history = db.get_feedback_history(complaint["id"], fb_map_data)
    if history:
        st.markdown("**跟踪反馈记录**")
        st.dataframe(_feedback_table(history, complaint), use_container_width=True,
                     hide_index=True)


# ============================================================
# 五、可复用表单(「根因措施」「跟踪反馈」「待处理任务」三处共用同一套逻辑)
# ============================================================

def render_action_form(complaint, key_prefix, debug=False):
    """根因结论 + 改善措施 表单。
    从"待分析"或"待措施"进入都走这里; 提交后跟踪期重新开始计算。"""
    old_root = complaint.get("root_cause") or ""
    old_action = complaint.get("action_description") or ""

    if complaint.get("status") == STATUS_ACTION:
        st.warning("该客诉需重新制定措施，提交后跟踪期（{}个月×{}次反馈）"
                   "重新开始计算".format(TRACK_MONTHS, FEEDBACK_ROUNDS))
    if old_root:
        st.caption("以下为上一版内容，可直接修改后提交")

    root_cause = st.text_area("根因结论 *", value=old_root, height=100,
                              key=key_prefix + "_root")
    action_description = st.text_area("改善措施描述 *", value=old_action,
                                      height=100, key=key_prefix + "_action")

    # 调试模式: 允许指定措施完成时间(正常情况固定为今天)
    action_date = None
    if debug:
        action_date = st.date_input("措施完成时间（调试模式）", value=date.today(),
                                    key=key_prefix + "_adate")
        st.caption("指定后，两次反馈截止日期按该日期 +1/+2 个月推算，"
                   "可用来验证逾期/即将到期提醒")

    if st.button("提交根因与措施", type="primary", use_container_width=True,
                 key=key_prefix + "_btn"):
        if not root_cause or not action_description:
            st.warning("根因结论和改善措施均为必填")
        else:
            try:
                action_date_str = action_date.isoformat() if (debug and action_date) else None
                ad, fb1, fb2 = db.submit_analysis_action(
                    complaint["id"], root_cause, action_description,
                    action_date_str, viewer=user)
                _clear_caches()
                st.success(
                    "✅ 已提交，进入跟踪期！\n\n"
                    "- 措施日期: **{}**\n"
                    "- 第1次反馈截止: **{}**(措施+1个月)\n"
                    "- 第2次反馈截止: **{}**(措施+2个月)".format(ad, fb1, fb2))
                st.rerun()
            except Exception as e:
                st.error("提交失败: {}".format(e))


def render_feedback_form(complaint, key_prefix, fb_map_data):
    """跟踪反馈表单: 自动判断本次是第几次反馈, 提交后按规则流转状态。"""
    done_fbs = db.round_feedbacks(complaint, fb_map_data)
    month_now = len(done_fbs) + 1
    due = complaint.get("feedback1_due") if month_now == 1 else complaint.get("feedback2_due")

    level = db.classify_deadline(due)
    st.markdown("**本次为第 {} 次反馈**，截止日期 **{}** {}".format(
        month_now, due or "-", _LEVEL_MARK.get(level, "")))

    # 本轮历史反馈回显
    if done_fbs:
        st.dataframe(_feedback_table(done_fbs, complaint), use_container_width=True,
                     hide_index=True)

    if month_now > FEEDBACK_ROUNDS:
        st.warning("本轮{}次反馈已提交，等待经理处理".format(FEEDBACK_ROUNDS))
        return

    col1, col2 = st.columns(2)
    with col1:
        is_effective = st.radio("措施是否有效", YES_NO, horizontal=True,
                                key=key_prefix + "_eff")
    with col2:
        has_similar = st.radio("是否收到同类投诉", YES_NO, horizontal=True,
                               key=key_prefix + "_sim")
    comment = st.text_area("反馈说明", height=80, key=key_prefix + "_cmt")

    if st.button("提交第{}次反馈".format(month_now), type="primary",
                 use_container_width=True, key=key_prefix + "_btn"):
        try:
            month, qualified = db.submit_feedback(
                complaint["id"], is_effective, has_similar, comment, viewer=user)
            _clear_caches()
            # 提示先存session_state再rerun: st.rerun()会丢弃当次渲染的消息,
            # 直接st.success/st.error的话提示会看不到(v0.4)
            if month >= FEEDBACK_ROUNDS and qualified:
                flash = ("success",
                         "✅ 两次反馈均达标，已自动转为 **待关闭**，等待经理确认")
            elif month >= FEEDBACK_ROUNDS:
                flash = ("error",
                         "❌ 反馈未达标（措施无效或出现同类投诉），已自动转为 **待措施**。\n\n"
                         "请到「根因措施」页重新制定措施，提交后跟踪期重新开始计算")
            elif qualified is False:
                # v0.4新规则: 第1次反馈不合格, 立即转待措施, 不再等第2次
                flash = ("error",
                         "❌ 本轮反馈不合格，请重新制定措施\n\n"
                         "请到「根因措施」页重新制定措施，提交后跟踪期"
                         "（{}个月×{}次反馈）重新开始计算".format(
                             TRACK_MONTHS, FEEDBACK_ROUNDS))
            else:
                flash = ("success",
                         "✅ 第1次反馈已提交，下次反馈截止: **{}**".format(
                             complaint.get("feedback2_due")))
            st.session_state["fb_flash"] = flash
            st.rerun()
        except Exception as e:
            st.error("提交失败: {}".format(e))


# ============================================================
# 六、标签页(按角色显示)
# ============================================================

# 页面按角色显示(权限清单见 config.ROLE_PAGES, 这里与它保持一致)
if not is_manager:      # 工程师: 登记 / 列表(仅自己) / 待处理 / 根因措施 / 跟踪反馈
    tabs = st.tabs(["🆕 新增客诉", "📋 客诉列表", "⏰ 待处理任务",
                    "🔬 根因措施", "📈 跟踪反馈"])
    tab_new, tab_list, tab_tasks, tab_action, tab_feedback = tabs
else:                   # 经理: 全部列表 / 待关闭确认 / 已关闭列表 / 分析看板
    tabs = st.tabs(["📋 客诉列表", "✅ 待关闭确认", "📁 已关闭列表", "📊 分析看板"])
    tab_list, tab_close, tab_closed, tab_board = tabs


# ---------- 页面1: 新增客诉(工程师) ----------
if not is_manager:
    with tab_new:
        st.subheader("新增客诉登记")
        st.info("只需填5项，系统自动：生成编号 / 算分析截止（收货日期+{}个自然日）/ "
                "状态置'{}'".format(ANALYSIS_DAYS, STATUS_ANALYSIS))

        col1, col2 = st.columns(2)
        with col1:
            customer_name = st.text_input("客户名称 *", key="new_customer")
            product_model = st.text_input("产品型号", key="new_model")
            problem_type = st.selectbox("问题类型", PROBLEM_TYPES, key="new_type")
        with col2:
            received_date = st.date_input("收货日期 *", value=date.today(),
                                          key="new_received")
            problem_description = st.text_area("问题描述 *", height=100,
                                               key="new_desc")

        # ---- 同类问题重复发生提示(8D的D7预防再发): 只警告不阻断 ----
        # 客户名称与问题类型都填了才查, 查的是近 SIMILAR_CHECK_MONTHS 个月内
        # 同客户 + 同问题类型的历史客诉(复用缓存, 不额外查库)
        similar = []
        if customer_name and problem_type:
            similar = db.find_similar_history(customer_name, problem_type)
        if similar:
            latest = similar[0]
            latest_month = str(latest.get("received_date") or "")[:7]
            # 中文月份展示: 2026-03 → 2026年3月
            if len(latest_month) == 7:
                latest_month = "{}年{}月".format(latest_month[:4],
                                                 int(latest_month[5:7]))
            st.warning(
                "⚠️ 该客户同类问题曾于 **{}** 发生（近{}个月内共 **{}** 条），"
                "建议横向展开排查。".format(
                    latest_month, SIMILAR_CHECK_MONTHS, len(similar)))
            with st.expander("查看历史同类客诉（{} 条，不阻断提交）".format(len(similar))):
                hist_rows = [{
                    "客诉编号": c["complaint_no"],
                    "收货日期": c.get("received_date") or "-",
                    "型号": c.get("product_model") or "-",
                    "问题类型": c.get("problem_type") or "-",
                    "状态": c.get("status") or "-",
                    "问题描述": (c.get("problem_description") or "")[:60],
                } for c in similar]
                st.dataframe(pd.DataFrame(hist_rows), use_container_width=True,
                             hide_index=True)

        if st.button("提交登记", type="primary", use_container_width=True,
                     key="new_submit"):
            if not customer_name or not problem_description:
                st.warning("客户名称和问题描述为必填项")
            else:
                try:
                    # 责任人自动记为当前登录人(数据隔离用; 老数据 owner_id 为空, 全员可见)
                    no = db.insert_complaint(
                        customer_name, product_model, problem_type,
                        problem_description, received_date.isoformat(),
                        owner_id=user["id"])
                    _clear_caches()
                    st.success(
                        "✅ 登记成功！客诉编号 **{}**\n\n"
                        "- 分析截止日期（收货+{}个自然日）: **{}**".format(
                            no, ANALYSIS_DAYS,
                            db.analysis_deadline_of(received_date.isoformat())))
                    # 提交后再次提示同类问题, 避免登记完就忘了横向展开
                    if similar:
                        st.warning(
                            "⚠️ 该客户同类问题近{}个月内还有 **{}** 条历史记录，"
                            "记得做横向展开排查。".format(
                                SIMILAR_CHECK_MONTHS, len(similar)))
                    st.rerun()
                except Exception as e:
                    st.error("登记失败: {}".format(e))


# ---------- 页面2: 客诉列表(工程师=仅自己, 经理=全部) ----------
with tab_list:
    st.subheader("客诉列表")
    if is_manager:
        st.caption("经理视图：可查看**全部**客诉（含所有工程师登记的）")
    else:
        st.caption("工程师视图：仅显示**本人登记**的客诉（系统上线前的历史数据也可见）")

    filter_status = st.selectbox("按状态筛选", ["全部"] + ALL_STATUS, key="list_filter")
    # 传 viewer: 工程师在 SQL 层就只查得到自己的数据, 不是"查出来再藏起来"
    data = db.load_complaints(None if filter_status == "全部" else filter_status,
                              viewer=user)

    if not data:
        st.info("暂无客诉记录")
    else:
        owner_names = db.get_account_names()
        # 按紧急度排序: 有截止日期的排前面(越早到期越靠前 → 逾期的自动置顶),
        # 已关闭等没有下一步截止日期的排最后
        data = sorted(data, key=lambda c: _urgency_key(c, fb_map))

        rows = []
        for seq, c in enumerate(data, start=1):
            due = _next_due(c, fb_map)
            owner_id = c.get("owner_id")
            rows.append({
                "序号": seq,
                "客诉编号": c["complaint_no"],
                "客户": c["customer_name"],
                "型号": c.get("product_model") or "-",
                "问题类型": c.get("problem_type") or "-",
                "责任人": owner_names.get(owner_id) if owner_id else "历史数据",
                "收货日期": c.get("received_date") or "-",
                "状态": c.get("status"),
                "下一步截止": due or "-",
                "提醒": _due_text(due) if due else "-",
                "措施日期": c.get("action_date") or "-",
                "反馈1截止": c.get("feedback1_due") or "-",
                "反馈2截止": c.get("feedback2_due") or "-",
            })
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

        # ---- 明细区: 可展开, 含进度条 + 删除按钮 ----
        st.markdown("---")
        st.markdown("**客诉详情**（点击展开查看进度条 / 删除）")
        for seq, c in enumerate(data, start=1):
            title = "{}. {} | {} | {}".format(
                seq, c["complaint_no"], c["customer_name"], c["status"])
            with st.expander(title):
                render_detail(c, fb_map)

                # ---- 删除流程: 删除按钮 → 二次确认 → 执行 ----
                confirm_key = "confirm_del_{}".format(c["id"])
                if st.button("🗑️ 删除该客诉", key="del{}".format(c["id"])):
                    st.session_state[confirm_key] = True

                if st.session_state.get(confirm_key):
                    st.warning(
                        "确认删除客诉 **{}** 吗？\n\n"
                        "该客诉的全部跟踪反馈记录将一并删除，不可恢复！".format(
                            c["complaint_no"]))
                    cc1, cc2 = st.columns(2)
                    if cc1.button("✅ 确认删除", key="delyes{}".format(c["id"]),
                                  type="primary"):
                        try:
                            # 传 viewer: db层会再校验一次归属
                            db.delete_complaint(c["id"], viewer=user)
                        except Exception as e:
                            st.error("删除失败: {}".format(e))
                        else:
                            st.session_state.pop(confirm_key, None)
                            _clear_caches()
                            st.success("已删除: {}".format(c["complaint_no"]))
                            st.rerun()
                    if cc2.button("❌ 取消", key="delno{}".format(c["id"])):
                        st.session_state.pop(confirm_key, None)
                        st.rerun()


# ---------- 页面3: 待处理任务(工程师) ----------
if not is_manager:
    with tab_tasks:
        st.subheader("待处理任务")
        st.caption("把逾期的、快到期的、需要重新整改的客诉汇总到一处，"
                   "选一条直接在下面填写，不用在页面之间来回切")

        # 传 viewer: 工程师只会看到自己负责的客诉产生的任务
        tasks = db.build_task_list(alerts, fb_map, viewer=user)
        if not tasks:
            st.success("✅ 没有待处理任务")
        else:
            # 任务标签: 紧急度图标 + 客诉编号 + 客户 + 该做什么 + 截止情况
            action_label = {
                "analysis": "填写根因与措施",
                "feedback": "提交跟踪反馈",
                "reopen": "重新制定措施",
            }
            level_icon = {"overdue": "🔴", "soon": "🟠", "todo": "🔵"}

            labels = []
            for t in tasks:
                c = t["complaint"]
                what = action_label[t["action"]]
                if t["action"] == "feedback" and t.get("feedback_no"):
                    what = "提交第{}次反馈".format(t["feedback_no"])
                tail = ""
                if t["days_left"] is not None:
                    tail = "已逾期{}天" if t["days_left"] < 0 else "还剩{}天"
                    tail = "（" + tail.format(abs(t["days_left"])) + "）"
                labels.append("{} {} | {} | {} {}".format(
                    level_icon[t["level"]], c["complaint_no"],
                    c["customer_name"], what, tail))

            sel_idx = st.selectbox("选择任务（按紧急度排序）",
                                   list(range(len(tasks))),
                                   format_func=lambda i: labels[i],
                                   key="task_sel")
            task = tasks[sel_idx]
            c = task["complaint"]

            st.markdown("---")
            render_detail(c, fb_map)
            st.markdown("---")

            # 内联处理: 根据任务类型直接渲染对应表单
            if task["action"] == "feedback":
                render_feedback_form(c, "task", fb_map)
            else:
                render_action_form(c, "task", debug_mode)


# ---------- 页面4: 根因措施(工程师) ----------
if not is_manager:
    with tab_action:
        st.subheader("填写根因结论与改善措施")
        st.info("提交后系统自动：记录措施日期 / 算两次反馈截止（措施日期+1、+2个月）/ "
                "状态→'{}'".format(STATUS_TRACKING))

        # 待分析(新登记) + 待措施(反馈未达标或经理退回) 都在这里处理
        # 传 viewer: 只会列出自己负责的客诉
        pool = (db.load_complaints(STATUS_ANALYSIS, viewer=user)
                + db.load_complaints(STATUS_ACTION, viewer=user))
        if not pool:
            st.success("✅ 没有待分析 / 待重新整改的客诉")
        else:
            options = {"{} | {} | {} | {}".format(
                c["complaint_no"], c["customer_name"], c.get("problem_type"),
                c.get("status")): c["id"] for c in pool}
            sel = st.selectbox("选择客诉", list(options.keys()), key="act_sel")
            c = next(x for x in pool if x["id"] == options[sel])
            render_action_form(c, "act", debug_mode)


# ---------- 页面5: 跟踪反馈(工程师) ----------
if not is_manager:
    with tab_feedback:
        st.subheader("上传跟踪反馈")
        st.info("每月一次，共{}次。两次均'措施有效=是'且'无同类投诉' → 自动转'{}'；"
                "任一次不达标 → 转'{}'重新整改（第1次就不合格立即转，不等第2次）".format(
                    FEEDBACK_ROUNDS, STATUS_PENDING_CLOSE, STATUS_ACTION))

        pool = db.load_complaints(STATUS_TRACKING, viewer=user)
        if not pool:
            st.info("暂无跟踪中的客诉")
        else:
            options = {"{} | {} | 措施日期{}".format(
                c["complaint_no"], c["customer_name"], c.get("action_date")): c["id"]
                for c in pool}
            sel = st.selectbox("选择客诉", list(options.keys()), key="fb_sel")
            c = next(x for x in pool if x["id"] == options[sel])
            render_feedback_form(c, "fbpage", fb_map)


# ---------- 页面6: 待关闭确认(经理) ----------
if is_manager:
    with tab_close:
        st.subheader("待关闭确认")
        pool = db.load_complaints(STATUS_PENDING_CLOSE, viewer=user)
        if not pool:
            st.success("✅ 暂无待确认关闭的客诉")
        else:
            st.warning("以下客诉两次反馈均达标，请确认关闭。发现异常可退回重新整改")

            for c in pool:
                fbs = db.round_feedbacks(c, fb_map)
                with st.expander("{} | {} | {}".format(
                        c["complaint_no"], c["customer_name"], c.get("problem_type"))):
                    st.markdown(_progress_bar(c["status"]), unsafe_allow_html=True)
                    st.markdown(
                        "- 根因: {}\n- 措施: {}\n- 措施日期: {}\n"
                        "- 反馈1: {}（有效={}，同类投诉={}）\n"
                        "- 反馈2: {}（有效={}，同类投诉={}）".format(
                            c.get("root_cause"), c.get("action_description"),
                            c.get("action_date"),
                            fbs[0]["feedback_date"] if len(fbs) > 0 else "-",
                            fbs[0]["is_effective"] if len(fbs) > 0 else "-",
                            fbs[0]["has_similar_complaint"] if len(fbs) > 0 else "-",
                            fbs[1]["feedback_date"] if len(fbs) > 1 else "-",
                            fbs[1]["is_effective"] if len(fbs) > 1 else "-",
                            fbs[1]["has_similar_complaint"] if len(fbs) > 1 else "-"))
                    if fbs:
                        st.dataframe(_feedback_table(fbs, c), use_container_width=True,
                                     hide_index=True)

                    col1, col2 = st.columns(2)
                    with col1:
                        if st.button("✅ 确认关闭", key="close{}".format(c["id"]),
                                     type="primary", use_container_width=True):
                            try:
                                # 传 viewer: db层校验必须是经理 + 本轮达标
                                db.close_complaint(c["id"], viewer=user)
                                _clear_caches()
                                st.success("已关闭: {}".format(c["complaint_no"]))
                                st.rerun()
                            except Exception as e:
                                # 校验不通过时明确提示, 不静默失败
                                st.error("无法关闭: {}".format(e))
                    with col2:
                        if st.button("↩️ 退回重改", key="reopen{}".format(c["id"]),
                                     use_container_width=True):
                            try:
                                db.reopen_to_action(c["id"], viewer=user)
                            except Exception as e:
                                st.error("退回失败: {}".format(e))
                            else:
                                _clear_caches()
                                st.info("已退回'{}'，跟踪期将在重新提交措施后开始".format(
                                    STATUS_ACTION))
                                st.rerun()


# ---------- 页面7: 已关闭列表(经理) ----------
if is_manager:
    with tab_closed:
        st.subheader("已关闭列表")
        closed = db.load_closed(viewer=user)

        if not closed:
            st.info("暂无已关闭的客诉")
        else:
            summary = db.close_summary()
            m1, m2, m3 = st.columns(3)
            m1.metric("累计关闭", summary["total"])
            m2.metric("本月关闭", summary["this_month"])
            m3.metric("平均闭环天数", summary["avg_days"])

            # 按关闭月份筛选(从数据的 closed_at 里取月份, 倒序排列)
            months = sorted({str(c.get("closed_at") or "")[:7]
                             for c in closed if c.get("closed_at")}, reverse=True)
            month_sel = st.selectbox("按关闭月份筛选", ["全部"] + months,
                                     key="closed_month")
            rows = [c for c in closed
                    if month_sel == "全部"
                    or str(c.get("closed_at") or "").startswith(month_sel)]

            table_rows = []
            for seq, c in enumerate(rows, start=1):
                # 闭环天数 = 关闭日期 - 收货日期(两个日期都有才算)
                span = "-"
                received = str(c.get("received_date") or "")
                closed_at = str(c.get("closed_at") or "")
                if len(received) >= 10 and len(closed_at) >= 10:
                    try:
                        span = (date.fromisoformat(closed_at[:10])
                                - date.fromisoformat(received[:10])).days
                    except ValueError:
                        span = "-"
                table_rows.append({
                    "序号": seq,
                    "客诉编号": c["complaint_no"],
                    "客户": c["customer_name"],
                    "型号": c.get("product_model") or "-",
                    "问题类型": c.get("problem_type") or "-",
                    "收货日期": c.get("received_date") or "-",
                    "措施日期": c.get("action_date") or "-",
                    "关闭时间": closed_at or "-",
                    "闭环天数": span,
                })
            df_closed = pd.DataFrame(table_rows)
            st.dataframe(df_closed, use_container_width=True, hide_index=True)

            # CSV导出(utf-8-sig 保证Excel打开中文不乱码)
            st.download_button(
                "⬇️ 导出CSV",
                data=df_closed.to_csv(index=False).encode("utf-8-sig"),
                file_name="已关闭客诉_{}.csv".format(date.today().isoformat()),
                mime="text/csv",
            )

            st.markdown("---")
            st.markdown("**整改详情**（点击展开查看完整闭环记录）")
            for c in rows:
                with st.expander("{} | {} | 关闭于 {}".format(
                        c["complaint_no"], c["customer_name"],
                        str(c.get("closed_at") or "")[:10])):
                    render_detail(c, fb_map)


# ---------- 页面8: 分析看板(仅经理) ----------
if is_manager:
    with tab_board:
        st.subheader("分析看板")

        # ---- 一次整改成功率 KPI: 衡量"措施一次就管用"的比例 ----
        st.markdown("**整改质量**")
        fps = db.first_pass_stats(fb_map)
        if not fps["has_data"]:
            st.info("暂无数据（还没有客诉进入跟踪期，制定措施后这里会开始统计）")
        else:
            k1, k2, k3, k4 = st.columns(4)
            k1.metric("一次整改成功率", "{}%".format(fps["pass_rate"]))
            k2.metric("重开率", "{}%".format(fps["reopen_rate"]))
            k3.metric("已进入跟踪期", fps["total"])
            k4.metric("重开过的客诉", fps["reopened"])
            st.caption(
                "口径：已进入跟踪期（制定过措施）共 **{}** 条，其中 **{}** 条发生过重开"
                "（经理退回重改，或第2次反馈未达标自动转待措施）。"
                "重开率 = {} ÷ {} = **{}%**，一次整改成功率 = 100% − 重开率 = **{}%**。"
                "同一条客诉重开多次只计 1 条。".format(
                    fps["total"], fps["reopened"], fps["reopened"], fps["total"],
                    fps["reopen_rate"], fps["pass_rate"]))
        st.markdown("---")

        by_type = db.count_by("problem_type")
        by_status = db.count_by("status")

        col1, col2 = st.columns(2)
        with col1:
            st.markdown("**按问题类型统计**")
            if by_type:
                fig = go.Figure(go.Bar(
                    x=list(by_type.values()), y=list(by_type.keys()),
                    orientation="h", marker_color=COLOR_HEX["pending"]))
                fig.update_layout(margin=dict(l=10, r=10, t=10, b=10), height=320)
                st.plotly_chart(fig, use_container_width=True)
            else:
                st.info("暂无数据")
        with col2:
            st.markdown("**按状态统计**")
            if by_status:
                fig = go.Figure(go.Bar(
                    x=list(by_status.keys()), y=list(by_status.values()),
                    marker_color=COLOR_HEX["ok"]))
                fig.update_layout(margin=dict(l=10, r=10, t=10, b=10), height=320)
                st.plotly_chart(fig, use_container_width=True)
            else:
                st.info("暂无数据")

        # 状态流转全景
        st.markdown("**状态分布总览**")
        total = sum(by_status.values()) if by_status else 0
        if total:
            cols_stat = st.columns(len(ALL_STATUS))
            for i, s in enumerate(ALL_STATUS):
                cols_stat[i].metric(s, by_status.get(s, 0))
        else:
            st.info("暂无数据")

        # 关闭趋势: 按月统计关闭量(最近 TREND_MONTHS 个月, 缺月补0)
        st.markdown("---")
        st.markdown("**关闭趋势**（最近 {} 个月）".format(TREND_MONTHS))
        trend = db.count_closed_by_month(TREND_MONTHS)
        if trend and any(n for _, n in trend):
            xs = [t[0] for t in trend]
            ys = [t[1] for t in trend]
            fig = go.Figure()
            fig.add_bar(x=xs, y=ys, name="当月关闭数", marker_color=COLOR_HEX["ok"])
            fig.add_scatter(x=xs, y=ys, name="趋势", mode="lines+markers",
                            line=dict(color=COLOR_HEX["pending"], width=2))
            fig.update_layout(margin=dict(l=10, r=10, t=10, b=10), height=320,
                              showlegend=False)
            st.plotly_chart(fig, use_container_width=True)
        else:
            st.info("暂无已关闭的客诉，关闭后这里会显示按月趋势")

        summary = db.close_summary()
        c1, c2, c3 = st.columns(3)
        c1.metric("累计关闭", summary["total"])
        c2.metric("本月关闭", summary["this_month"])
        c3.metric("平均闭环天数（收货→关闭）", summary["avg_days"])
