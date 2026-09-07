"""
smoke_test.py - 8D客诉整改跟踪系统 核心规则自检脚本
------------------------------------------------
用法(命令行, 不需要启动Streamlit, 也不连数据库):
    cd D:\\8d_system
    python smoke_test.py

检查内容(全部是纯逻辑, 不读写Turso):
  1. 状态常量 / 配置一致性
  2. 日期计算: 月份加减月末溢出、分析截止(收货+N天)、两次反馈截止
  3. 提醒判定: 逾期 / 即将到期 / 正常 / 无截止日期 四种边界
  4. 达标判定: 一轮反馈是否算"达标"
  5. 状态流转: 第1次合格等第2次、第1次不合格立即转待措施(v0.4)、
     第2次达标转待关闭、第2次未达标转待措施
  6. 轮次隔离: 重新制定措施/经理退回后, 上一轮反馈必须被排除(Bug2修复验证)
  7. 完整闭环模拟: 登记 → 措施 → 反馈×2 → 待关闭 → 经理关闭
  8. 异常闭环模拟: 反馈未达标 → 待措施 → 重新整改 → 跟踪期重新开始

有失败会打印明细并以退出码1结束, 方便以后改规则后做回归。
输出全部用ASCII标记([PASS]/[FAIL]), 避免Windows控制台编码问题。
"""
import sys
from datetime import date, timedelta

try:
    import db
    from config import (
        ANALYSIS_DAYS, REMIND_BEFORE_DAYS, FEEDBACK_ROUNDS, TRACK_MONTHS,
        SIMILAR_CHECK_MONTHS, ROLE_CODE_MANAGER, ROLE_CODE_ENGINEER,
        YES, NO, YES_NO, ALL_STATUS,
        STATUS_ANALYSIS, STATUS_ACTION, STATUS_TRACKING,
        STATUS_PENDING_CLOSE, STATUS_CLOSED,
        PROBLEM_TYPES, STATUS_TIPS,
    )
except Exception as e:      # 导入失败直接退出, 后面全部用例都没有意义
    print("[FAIL] 导入项目模块失败: {}".format(e))
    sys.exit(1)


# ---------------- 极简测试框架 ----------------
PASSED = 0
FAILED = 0
FAIL_MSGS = []


def check(name, condition, detail=""):
    """断言一条规则: 通过计数, 失败记录原因"""
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print("  [PASS] {}".format(name))
    else:
        FAILED += 1
        msg = "  [FAIL] {} {}".format(name, detail)
        FAIL_MSGS.append(msg)
        print(msg)


def fb(month, feedback_date, effective=YES, similar=NO, void=False):
    """构造一条反馈记录(模拟库里读出来的字典)"""
    return {
        "feedback_month": -abs(month) if void else abs(month),
        "feedback_date": feedback_date,
        "is_effective": effective,
        "has_similar_complaint": similar,
        "is_void": void,
    }


def iso(days_from_today):
    """今天偏移N天后的ISO日期字符串"""
    return (date.today() + timedelta(days=days_from_today)).isoformat()


# ============================================================
print("\n[1] 配置与状态常量")
# ============================================================
check("五种状态齐全且顺序为流程顺序",
      ALL_STATUS == [STATUS_ANALYSIS, STATUS_ACTION, STATUS_TRACKING,
                     STATUS_PENDING_CLOSE, STATUS_CLOSED],
      "实际: {}".format(ALL_STATUS))
check("问题类型为六选一", len(PROBLEM_TYPES) == 6, "实际: {}".format(PROBLEM_TYPES))
check("每个状态都有操作提示文案",
      all(s in STATUS_TIPS for s in ALL_STATUS),
      "缺失: {}".format([s for s in ALL_STATUS if s not in STATUS_TIPS]))
check("是/否选项顺序为[是, 否]", YES_NO == [YES, NO], "实际: {}".format(YES_NO))
check("跟踪期月数与反馈次数一致", TRACK_MONTHS == FEEDBACK_ROUNDS == 2,
      "TRACK_MONTHS={}, FEEDBACK_ROUNDS={}".format(TRACK_MONTHS, FEEDBACK_ROUNDS))


# ============================================================
print("\n[2] 日期计算规则")
# ============================================================
d = date(2026, 1, 31)
check("1月31日 +1个月 = 2月28日(平年月末溢出)",
      db.add_months(d, 1) == date(2026, 2, 28),
      "实际: {}".format(db.add_months(d, 1)))
check("1月31日 +2个月 = 3月31日",
      db.add_months(d, 2) == date(2026, 3, 31),
      "实际: {}".format(db.add_months(d, 2)))
check("12月31日 +2个月 = 次年2月28日(跨年月末溢出)",
      db.add_months(date(2026, 12, 31), 2) == date(2027, 2, 28),
      "实际: {}".format(db.add_months(date(2026, 12, 31), 2)))
check("1月15日 -1个月 = 上年12月15日(负数月份)",
      db.add_months(date(2026, 1, 15), -1) == date(2025, 12, 15),
      "实际: {}".format(db.add_months(date(2026, 1, 15), -1)))

check("分析截止 = 收货日期 + {}个自然日".format(ANALYSIS_DAYS),
      db.analysis_deadline_of("2026-08-01") == "2026-08-08",
      "实际: {}".format(db.analysis_deadline_of("2026-08-01")))

_action_d = date(2026, 8, 5)
check("第1次反馈截止 = 措施日期 +1个月",
      db.add_months(_action_d, 1).isoformat() == "2026-09-05")
check("第2次反馈截止 = 措施日期 +2个月",
      db.add_months(_action_d, 2).isoformat() == "2026-10-05")


# ============================================================
print("\n[3] 提醒判定边界(REMIND_BEFORE_DAYS={})".format(REMIND_BEFORE_DAYS))
# ============================================================
check("昨天到期 -> 已逾期", db.classify_deadline(iso(-1)) == "overdue")
check("今天到期 -> 即将到期(当天不算逾期)", db.classify_deadline(iso(0)) == "soon")
check("{}天后到期 -> 即将到期".format(REMIND_BEFORE_DAYS),
      db.classify_deadline(iso(REMIND_BEFORE_DAYS)) == "soon")
check("{}天后到期 -> 正常(还不提醒)".format(REMIND_BEFORE_DAYS + 1),
      db.classify_deadline(iso(REMIND_BEFORE_DAYS + 1)) == "normal")
check("没有截止日期 -> none", db.classify_deadline(None) == "none")
check("脏数据日期 -> none(不抛异常)", db.classify_deadline("not-a-date") == "none")


# ============================================================
print("\n[4] 达标判定: 两条都要 有效=是 且 同类投诉=否")
# ============================================================
check("没有任何反馈 -> 不达标", db.is_round_qualified([]) is False)
check("只交1条 -> 不达标",
      db.is_round_qualified([fb(1, "2026-09-05")]) is False)
check("2条都达标 -> 达标",
      db.is_round_qualified([fb(1, "2026-09-05"), fb(2, "2026-10-05")]) is True)
check("有1条'措施有效=否' -> 不达标",
      db.is_round_qualified([fb(1, "2026-09-05"),
                             fb(2, "2026-10-05", effective=NO)]) is False)
check("有1条'出现同类投诉' -> 不达标",
      db.is_round_qualified([fb(1, "2026-09-05"),
                             fb(2, "2026-10-05", similar=YES)]) is False)


# ============================================================
print("\n[5] 状态流转(v0.4: 第1次不合格立即转待措施; 轮末未达标也转待措施)")
# ============================================================
check("第1次反馈合格 -> 跟踪中(等第2次)",
      db.decide_status_after_feedback(1, [fb(1, "2026-09-05")]) == STATUS_TRACKING)
check("第1次反馈'措施有效=否' -> 立即转待措施(不等第2次)",
      db.decide_status_after_feedback(
          1, [fb(1, "2026-09-05", effective=NO)]) == STATUS_ACTION)
check("第1次反馈'同类投诉=是' -> 立即转待措施(不等第2次)",
      db.decide_status_after_feedback(
          1, [fb(1, "2026-09-05", similar=YES)]) == STATUS_ACTION)
check("第2次反馈且达标 -> 待关闭",
      db.decide_status_after_feedback(
          2, [fb(1, "2026-09-05"), fb(2, "2026-10-05")]) == STATUS_PENDING_CLOSE)
check("第2次反馈但不达标 -> 待措施(不再卡在跟踪中)",
      db.decide_status_after_feedback(
          2, [fb(1, "2026-09-05"),
              fb(2, "2026-10-05", effective=NO)]) == STATUS_ACTION)


# ============================================================
print("\n[6] 轮次隔离(Bug2修复: 上一轮反馈必须被排除)")
# ============================================================
# 场景: 经理退回当天工程师就重新提交措施, 新action_date正好等于上轮第2次反馈的日期
c = {"id": 1, "action_date": "2026-09-01"}
fb_map = {
    1: [
        fb(1, "2026-08-01", void=True),
        fb(2, "2026-09-01", void=True),   # 日期 == 新措施日期(最容易被误判的一条)
        fb(1, "2026-10-01"),              # 新一轮的第1次反馈
    ]
}
got = db.round_feedbacks(c, fb_map)
check("作废轮次被排除, 只保留本轮1条",
      len(got) == 1 and got[0]["feedback_date"] == "2026-10-01",
      "实际返回: {}".format([g["feedback_date"] for g in got]))

# 未作废 + 日期早于本轮措施日期(例如历史遗留数据)也应排除
c2 = {"id": 2, "action_date": "2026-10-01"}
fb_map2 = {2: [fb(1, "2026-08-01"), fb(2, "2026-09-01"), fb(3, "2026-11-01")]}
got2 = db.round_feedbacks(c2, fb_map2)
check("早于本轮措施日期的反馈被排除",
      len(got2) == 1 and got2[0]["feedback_date"] == "2026-11-01",
      "实际返回: {}".format([g["feedback_date"] for g in got2]))

check("没有措施日期时不返回任何反馈", db.round_feedbacks({"id": 3}, {}) == [])


# ============================================================
print("\n[7] 完整闭环模拟(登记 -> 措施 -> 反馈x2 -> 待关闭)")
# ============================================================
received = "2026-08-01"
deadline = db.analysis_deadline_of(received)
check("登记: 分析截止计算正确", deadline == "2026-08-08", "实际: {}".format(deadline))

complaint = {"id": 10, "status": STATUS_ANALYSIS, "action_date": None}
action_date = "2026-08-05"
complaint["action_date"] = action_date
fb1_due = db.add_months(date.fromisoformat(action_date), 1).isoformat()
fb2_due = db.add_months(date.fromisoformat(action_date), 2).isoformat()
complaint["status"] = STATUS_TRACKING
check("措施: 状态进入跟踪中", complaint["status"] == STATUS_TRACKING)
check("措施: 反馈1截止 = {}".format(fb1_due), fb1_due == "2026-09-05")
check("措施: 反馈2截止 = {}".format(fb2_due), fb2_due == "2026-10-05")

round_fbs = []
_fb_map = {10: round_fbs}

# 第1次反馈
month = len(db.round_feedbacks(complaint, _fb_map)) + 1
round_fbs.append(fb(month, "2026-09-05"))
complaint["status"] = db.decide_status_after_feedback(
    month, db.round_feedbacks(complaint, _fb_map))
check("第1次反馈: 序号=1 且 状态仍为跟踪中",
      month == 1 and complaint["status"] == STATUS_TRACKING,
      "实际: month={}, status={}".format(month, complaint["status"]))

# 第2次反馈(达标)
month = len(db.round_feedbacks(complaint, _fb_map)) + 1
round_fbs.append(fb(month, "2026-10-05"))
cur = db.round_feedbacks(complaint, _fb_map)
complaint["status"] = db.decide_status_after_feedback(month, cur)
check("第2次反馈达标: 状态转待关闭",
      month == 2 and complaint["status"] == STATUS_PENDING_CLOSE,
      "实际: month={}, status={}".format(month, complaint["status"]))

check("待关闭状态下达标校验通过(允许经理关闭)",
      db.is_round_qualified(db.round_feedbacks(complaint, _fb_map)) is True)


# ============================================================
print("\n[8] 异常闭环模拟(反馈未达标 -> 待措施 -> 重新整改 -> 跟踪期重启)")
# ============================================================
complaint2 = {"id": 11, "status": STATUS_TRACKING, "action_date": "2026-08-05"}
fbs2 = [fb(1, "2026-09-05"), fb(2, "2026-10-05", effective=NO)]
complaint2["status"] = db.decide_status_after_feedback(2, fbs2)
check("第2次反馈未达标: 状态转待措施", complaint2["status"] == STATUS_ACTION,
      "实际: {}".format(complaint2["status"]))

# 工程师重新提交措施: 新措施日期 + 上一轮反馈全部作废
new_action_date = "2026-10-10"
fbs2 = [fb(1, "2026-09-05", void=True), fb(2, "2026-10-05", void=True)]
complaint2["action_date"] = new_action_date
complaint2["status"] = STATUS_TRACKING
new_fb1 = db.add_months(date.fromisoformat(new_action_date), 1).isoformat()
new_fb2 = db.add_months(date.fromisoformat(new_action_date), 2).isoformat()
check("重新整改: 新反馈1截止按新措施日期重算",
      new_fb1 == "2026-11-10", "实际: {}".format(new_fb1))
check("重新整改: 新反馈2截止按新措施日期重算",
      new_fb2 == "2026-12-10", "实际: {}".format(new_fb2))
check("重新整改: 上一轮反馈全部作废, 本轮从0条开始",
      db.round_feedbacks(complaint2, {11: fbs2}) == [],
      "实际: {}".format(db.round_feedbacks(complaint2, {11: fbs2})))
check("重新整改: 下一次提交记为第1次反馈",
      len(db.round_feedbacks(complaint2, {11: fbs2})) + 1 == 1)

# 经理从"待关闭"退回, 效果应与上面一致
complaint3 = {"id": 12, "status": STATUS_PENDING_CLOSE, "action_date": "2026-08-05"}
fbs3 = [fb(1, "2026-09-05", void=True), fb(2, "2026-10-05", void=True)]
complaint3["action_date"] = "2026-10-10"
complaint3["status"] = STATUS_TRACKING
check("经理退回后重新整改: 本轮反馈同样从0条开始",
      db.round_feedbacks(complaint3, {12: fbs3}) == [])


# ============================================================
print("\n[9] 一次整改成功率(重开率)统计口径")
# ============================================================
# 用桩数据替换数据源, 保证不连库
_ORIG_LOAD = db.load_complaints
_ORIG_FBMAP = db.get_all_feedbacks


def _stub(complaints, feedback_map):
    """把客诉/反馈数据源替换成内存桩数据(不连库)"""
    db.load_complaints = lambda *a, **k: complaints
    db.get_all_feedbacks = lambda *a, **k: feedback_map


def _restore():
    """恢复真实数据源"""
    db.load_complaints = _ORIG_LOAD
    db.get_all_feedbacks = _ORIG_FBMAP


# 场景: 5条客诉, 其中4条已进入跟踪期(有action_date);
#       id=2 重开过2次(3条反馈里2条作废), id=4 重开过1次, id=99 是已删除客诉的残留反馈
_stub_complaints = [
    {"id": 1, "action_date": "2026-01-01"},
    {"id": 2, "action_date": "2026-02-01"},
    {"id": 3, "action_date": "2026-03-01"},
    {"id": 4, "action_date": "2026-04-01"},
    {"id": 5},                                  # 未进入跟踪期, 不计入分母
]
_stub_fbmap = {
    1: [fb(1, "2026-02-01"), fb(2, "2026-03-01")],
    2: [fb(1, "2026-03-01", void=True), fb(2, "2026-04-01", void=True),
        fb(3, "2026-05-01")],
    3: [fb(1, "2026-04-01"), fb(2, "2026-05-01")],
    4: [fb(1, "2026-05-01", void=True)],
    99: [fb(1, "2026-01-01", void=True)],       # 客诉已删除, 不应计入分子
}
# 先测"没有任何客诉进入跟踪期"的空数据场景
_stub([], {})
check("没有任何客诉进入跟踪期 -> has_data=False, 页面显示'暂无数据'",
      db.first_pass_stats({})["has_data"] is False)

# 再测有数据的场景
_stub(_stub_complaints, _stub_fbmap)
s = db.first_pass_stats(_stub_fbmap)
check("分母 = 已进入跟踪期的客诉数(4条, 不含未制定措施的)",
      s["total"] == 4, "实际: {}".format(s["total"]))
check("分子 = 重开过的客诉条数(2条: id2、id4)",
      s["reopened"] == 2, "实际: {}".format(s["reopened"]))
check("同一条客诉重开多次只计1条(重开率不会超100%)",
      s["reopened"] == 2 and s["reopen_rate"] <= 100.0,
      "实际: reopened={}, rate={}".format(s["reopened"], s["reopen_rate"]))
check("已删除客诉的残留作废反馈不计入分子",
      s["reopen_rate"] == 50.0, "实际: {}".format(s["reopen_rate"]))
check("重开率 = 分子/分母 x100% = 50.0%",
      s["reopen_rate"] == 50.0, "实际: {}".format(s["reopen_rate"]))
check("一次整改成功率 = 100% - 重开率 = 50.0%",
      s["pass_rate"] == 50.0, "实际: {}".format(s["pass_rate"]))

_restore()


# ============================================================
print("\n[10] 反馈提交时效(按时 / 逾期)")
# ============================================================
c_due = {"feedback1_due": "2026-09-05", "feedback2_due": "2026-10-05"}
check("截止日当天提交 -> 按时提交(绿)",
      db.feedback_timeliness(fb(1, "2026-09-05"), c_due) == ("按时提交", "ok"))
check("提前提交 -> 按时提交(绿)",
      db.feedback_timeliness(fb(1, "2026-09-01"), c_due) == ("按时提交", "ok"))
check("晚1天 -> 逾期1天提交(红)",
      db.feedback_timeliness(fb(1, "2026-09-06"), c_due) == ("逾期1天提交", "late"))
check("第2次反馈按 feedback2_due 判定",
      db.feedback_timeliness(fb(2, "2026-10-05"), c_due) == ("按时提交", "ok"))
check("第2次晚15天 -> 逾期15天提交(红)",
      db.feedback_timeliness(fb(2, "2026-10-20"), c_due) == ("逾期15天提交", "late"))
check("已作废的历史轮次 -> 历史轮次(灰, 截止日期已被覆盖不做无依据判断)",
      db.feedback_timeliness(fb(1, "2026-09-05", void=True), c_due)
      == ("历史轮次", "unknown"))
check("缺截止日期 -> '-'(灰, 不抛异常)",
      db.feedback_timeliness(fb(1, "2026-09-05"), {"feedback2_due": None})
      == ("-", "unknown"))


# ============================================================
print("\n[11] 同类问题重复发生检查(近{}个月)".format(SIMILAR_CHECK_MONTHS))
# ============================================================
_today = date.today()
_recent = (_today - timedelta(days=30)).isoformat()          # 1个月前, 在窗口内
_older = db.add_months(_today, -18).isoformat()               # 18个月前, 窗口外
_stub([
    {"id": 1, "complaint_no": "TS-2026-0001", "customer_name": "华东客户",
     "problem_type": "尺寸超差", "received_date": _recent, "status": "已关闭",
     "product_model": "M-100", "problem_description": "孔径偏大"},
    {"id": 2, "complaint_no": "TS-2026-0002", "customer_name": "华东客户",
     "problem_type": "表面缺陷", "received_date": _recent, "status": "已关闭",
     "product_model": "M-100", "problem_description": "划伤"},
    {"id": 3, "complaint_no": "TS-2025-0001", "customer_name": "华东客户",
     "problem_type": "尺寸超差", "received_date": _older, "status": "已关闭",
     "product_model": "M-100", "problem_description": "孔径偏大(旧)"},
    {"id": 4, "complaint_no": "TS-2026-0003", "customer_name": "华南客户",
     "problem_type": "尺寸超差", "received_date": _recent, "status": "已关闭",
     "product_model": "M-200", "problem_description": "孔径偏大"},
], {})

hit = db.find_similar_history("华东客户", "尺寸超差")
check("同客户+同问题类型+窗口内 -> 命中1条(超12个月的旧记录被排除)",
      len(hit) == 1 and hit[0]["id"] == 1,
      "实际命中: {}".format([h["id"] for h in hit]))
check("同客户+不同类型 -> 不命中",
      db.find_similar_history("华东客户", "材料异常") == [])
check("同类型+不同客户 -> 不命中",
      db.find_similar_history("新客户", "尺寸超差") == [])
check("客户名称为空 -> 返回空(不做无意义查询)",
      db.find_similar_history("", "尺寸超差") == [])
check("exclude_id 生效(排除自己)",
      db.find_similar_history("华东客户", "尺寸超差", exclude_id=1) == [])
check("结果按收货日期倒序(最近发生排最前)",
      [h["received_date"] for h in hit] == sorted(
          [h["received_date"] for h in hit], reverse=True))

_restore()


# ============================================================
print("\n[12] 密码哈希与密码策略")
# ============================================================
_hash = db.hash_password("Abc123456")
check("哈希格式为 pbkdf2_sha256$迭代次数$盐$哈希(4段)",
      len(_hash.split("$")) == 4 and _hash.startswith("pbkdf2_sha256$"),
      "实际: {}".format(_hash[:40] + "..."))
check("明文密码不出现在哈希里", "Abc123456" not in _hash)
check("正确密码校验通过", db.verify_password("Abc123456", _hash) is True)
check("错一位密码校验失败", db.verify_password("Abc123457", _hash) is False)
check("空密码校验失败", db.verify_password("", _hash) is False)
check("哈希字段为空时校验失败(不抛异常)", db.verify_password("x", "") is False)
check("哈希格式被篡改时校验失败(不抛异常)",
      db.verify_password("x", "not-a-hash") is False)
check("同一密码两次哈希的盐不同(防彩虹表)",
      db.hash_password("Abc123456") != db.hash_password("Abc123456"))
check("自带的盐也能正确校验(哈希可迁移/可重算)",
      db.verify_password("Abc123456", db.hash_password("Abc123456", 1000,
                                                      "aabbcc")) is True)

check("密码少于6位被拒", db.validate_password("Ab1")[0] is False)
check("纯数字被拒(必须含字母)", db.validate_password("123456")[0] is False)
check("纯字母被拒(必须含数字)", db.validate_password("abcdef")[0] is False)
check("字母+数字且满6位通过", db.validate_password("abc123")[0] is True)
check("密码与账号相同被拒", db.validate_password("engineer", "engineer")[0] is False)
_pwd = db.gen_random_password()
check("随机初始密码满足密码策略",
      db.validate_password(_pwd)[0] is True and len(_pwd) >= 6,
      "实际: 长度{}".format(len(_pwd)))


# ============================================================
print("\n[13] 角色判定与数据权限")
# ============================================================
_mgr = {"id": 1, "username": "manager", "role_code": ROLE_CODE_MANAGER}
_eng = {"id": 2, "username": "engineer", "role_code": ROLE_CODE_ENGINEER}
_eng2 = {"id": 3, "username": "engineer2", "role_code": ROLE_CODE_ENGINEER}
_c_own = {"id": 10, "owner_id": 2}      # 工程师自己的
_c_other = {"id": 11, "owner_id": 3}    # 别人的
_c_legacy = {"id": 12, "owner_id": None}  # 历史数据(初始化前的老客诉)

check("经理能看所有客诉(自己的/别人的/历史数据)",
      db.can_access(_c_own, _mgr) and db.can_access(_c_other, _mgr)
      and db.can_access(_c_legacy, _mgr))
check("工程师能看自己的客诉", db.can_access(_c_own, _eng) is True)
check("工程师不能看别人的客诉(数据隔离)", db.can_access(_c_other, _eng) is False)
check("工程师2不能看工程师1的客诉", db.can_access(_c_own, _eng2) is False)
check("历史数据(owner_id为空)全员可见", db.can_access(_c_legacy, _eng) is True)
check("未登录(不传user)不做限制(兼容初始化脚本/自检)",
      db.can_access(_c_other, None) is True)

check("角色码: 英文engineer能被识别",
      db.role_code_of(_eng) == ROLE_CODE_ENGINEER)
check("角色码: 英文manager能被识别",
      db.role_code_of(_mgr) == ROLE_CODE_MANAGER)
check("角色码: 传入中文'经理'也能被识别(兼容)",
      db.role_code_of({"role": "经理"}) == ROLE_CODE_MANAGER)
check("角色码: 空用户/未知角色识别为None",
      db.role_code_of(None) is None and db.role_code_of({}) is None)
check("is_manager: 经理为True 工程师为False",
      db.is_manager(_mgr) is True and db.is_manager(_eng) is False)


# ============================================================
print("\n" + "=" * 56)
print("自检结果: 通过 {} 条, 失败 {} 条".format(PASSED, FAILED))
if FAILED:
    print("-" * 56)
    for m in FAIL_MSGS:
        print(m)
    print("=" * 56)
    sys.exit(1)
print("全部通过 [ALL PASS]")
print("=" * 56)
sys.exit(0)
