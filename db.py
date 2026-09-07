"""
db.py - 8D客诉整改跟踪系统 Turso数据库操作层
------------------------------------------------
连接方式与 LPA 系统完全一致(urllib直调 /v2/pipeline), 产线环境验证过。
表结构参考 ERPNext Quality 模块(root_cause/action_type/status) + 简道云闭环流程。

两张核心表:
  complaints  客诉主表 —— 每条客诉一行, 存当前状态和自动计算的截止日期
  feedbacks   措施跟踪反馈表 —— 每次反馈一行, 一个客诉跟踪期内最多2条

状态机(v0.2):
  待分析 → 跟踪中 → 待关闭 → 已关闭
              ↑   ↘ 待措施 ↗
  反馈未达标 或 经理退回 → 待措施(重新制定措施, 跟踪期重新开始)

v0.2 关键改动:
  1) 反馈未达标自动转"待措施"(决策Q1-A), 修复客诉卡死在"跟踪中"的Bug
  2) 重新制定措施时把上一轮反馈的 feedback_month 置负作废, 修复轮次串号Bug
  3) 提醒/列表改为"全表一次查询 + 内存过滤", 消灭N+1远程请求

v0.4 关键改动:
  反馈判定规则修订: 第1次反馈就不合格(有效=否 或 同类=是)时立即转"待措施"
  并作废本轮反馈(置负留痕), 不再等第2次; 第2次不合格仍按原规则整轮判定。
"""
import calendar
import hashlib
import hmac
import json
import re
import secrets
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta

import streamlit as st

from config import (
    ANALYSIS_DAYS,       # 分析期限(天): 分析截止 = 收货日期 + N天
    REMIND_BEFORE_DAYS,  # 到期前N天开始提醒(即将到期)
    FEEDBACK_ROUNDS,     # 一个跟踪期内需要提交几次反馈
    SIMILAR_CHECK_MONTHS,  # 同类问题重复发生检查的回溯月数
    YES, NO,             # 反馈判定用的"是"/"否"字面量
    STATUS_ANALYSIS,     # 待分析
    STATUS_ACTION,       # 待措施(需重新制定措施)
    STATUS_TRACKING,     # 跟踪中
    STATUS_PENDING_CLOSE,  # 待关闭(等经理确认)
    STATUS_CLOSED,       # 已关闭
    ROLE_CODE_ENGINEER, ROLE_CODE_MANAGER, ROLE_CODES,
    ROLE_CODE_TO_CN, ROLE_CN_TO_CODE,
    PASSWORD_MIN_LEN, PASSWORD_ITERATIONS,
)

# ============================================================
# ★★★ Turso 数据库连接配置区 ★★★
# ============================================================
# 令牌不写在代码里, 从 .streamlit/secrets.toml 读取(该文件已被.gitignore排除):
#   TURSO_8D_TOKEN = "你的turso auth token"
TURSO_URL = ""   # 例: "libsql://你的库名.turso.io"  ← 部署/推送时也可留空从此行下方secrets读取
TURSO_TOKEN_ENV_KEY = "TURSO_8D_TOKEN"   # secrets.toml 里的键名

if not TURSO_URL:
    try:
        TURSO_URL = st.secrets["TURSO_8D_URL"]
    except Exception:
        TURSO_URL = ""
TURSO_AUTH_TOKEN = ""
try:
    TURSO_AUTH_TOKEN = st.secrets[TURSO_TOKEN_ENV_KEY]
except Exception:
    TURSO_AUTH_TOKEN = ""
# ============================================================


# ------------------------------------------------------------
# 工具函数
# ------------------------------------------------------------

def add_months(d, months):
    """日期加N个自然月, 日溢出自动取当月最后一天。
    例: 2026-01-31 + 1个月 = 2026-02-28。"""
    y = d.year + (d.month - 1 + months) // 12
    m = (d.month - 1 + months) % 12 + 1
    last_day = calendar.monthrange(y, m)[1]
    return date(y, m, min(d.day, last_day))


def _today_str():
    """今天的日期字符串(ISO格式, 与库内日期字段一致)"""
    return date.today().isoformat()


def _now_str():
    """当前时间字符串(与库内 created_at / last_login_at 等字段格式一致)"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ------------------------------------------------------------
# 纯函数区(不查库, 只做规则判定)
# 把规则单独抽出来, 一是业务代码只用一处, 二是自检脚本可以直接测
# ------------------------------------------------------------

def classify_deadline(due_str, today=None):
    """判定某个截止日期当前处于什么状态(提醒规则的唯一出处)。
    返回4种结果:
      'none'    —— 没有截止日期(还没进入该阶段)
      'overdue' —— 已逾期: 当前日期 > 截止日期
      'soon'    —— 即将到期: 距离截止日期 <= REMIND_BEFORE_DAYS 天(含当天)
      'normal'  —— 正常: 还早
    注意: 截止日当天算'即将到期'不算逾期, 与业务规则"当前日期 > 截止日期"一致。"""
    if not due_str:
        return "none"
    try:
        due = date.fromisoformat(str(due_str)[:10])
    except (ValueError, TypeError):
        return "none"
    today = today or date.today()
    days_left = (due - today).days
    if days_left < 0:
        return "overdue"
    if days_left <= REMIND_BEFORE_DAYS:
        return "soon"
    return "normal"


def is_round_qualified(feedbacks, rounds=FEEDBACK_ROUNDS):
    """判定一轮跟踪反馈是否达标(纯函数)。
    达标 = 条数够(默认2条) 且 每条都是"措施有效=是"且"未收到同类投诉=否"。"""
    if not feedbacks or len(feedbacks) < rounds:
        return False
    return all(
        fb.get("is_effective") == YES and fb.get("has_similar_complaint") == NO
        for fb in feedbacks[:rounds]
    )


def decide_status_after_feedback(month, feedbacks, rounds=FEEDBACK_ROUNDS):
    """提交第 month 次反馈后, 客诉应流转到哪个状态(纯函数)。
    规则(v0.4修订):
      1) 本轮反馈中只要有一条"措施有效=否"或"同类投诉=是" → 立即转待措施
         (第1次就不合格时不再等第2次, 避免白等一个月跟踪期)
      2) 无不合格且未到轮末 → 跟踪中(等下一次反馈)
      3) 轮末(第2次)且本轮达标 → 待关闭(等经理确认)
      4) 轮末但不达标 → 待措施(已被规则1覆盖, 兜底保留)
    这样工程师在「根因措施」页一定能选到它, 不会出现卡死在"跟踪中"的情况。"""
    if any(fb.get("is_effective") == NO or fb.get("has_similar_complaint") == YES
           for fb in (feedbacks or [])):
        return STATUS_ACTION
    if month < rounds:
        return STATUS_TRACKING
    return STATUS_PENDING_CLOSE if is_round_qualified(feedbacks, rounds) else STATUS_ACTION


def _to_turso_arg(value):
    """Python值 → Turso API参数格式。
    类型规则: int/bool→integer(字符串), float→float, text→text, None→null。"""
    if value is None:
        return {"type": "null"}
    if isinstance(value, bool):   # bool必须在int之前判断(True也是int)
        return {"type": "integer", "value": "1" if value else "0"}
    if isinstance(value, int):
        return {"type": "integer", "value": str(value)}
    if isinstance(value, float):
        return {"type": "float", "value": value}
    return {"type": "text", "value": str(value)}


def _from_turso_value(val):
    """Turso返回值 → Python值"""
    if val is None:
        return None
    t = val.get("type")
    if t == "null":
        return None
    if t == "integer":
        return int(val.get("value", 0))
    if t == "float":
        return float(val.get("value", 0.0))
    return val.get("value", "")


def _get_http_url():
    """把 libsql:// 地址转换成 HTTP pipeline 接口地址"""
    url = TURSO_URL
    if url.startswith("libsql://"):
        url = "https://" + url[len("libsql://"):]
    if url.endswith("/"):
        url = url[:-1]
    return url + "/v2/pipeline"


def _execute(sql, args=None):
    """执行单条SQL, 返回(列名列表, 行列表)。失败抛RuntimeError(中文信息)。"""
    turso_args = [_to_turso_arg(a) for a in args] if args else []
    body = json.dumps({
        "requests": [
            {"type": "execute", "stmt": {"sql": sql, "args": turso_args}},
            {"type": "close"},
        ]
    }).encode("utf-8")
    req = urllib.request.Request(
        _get_http_url(),
        data=body,
        headers={
            "Authorization": "Bearer {}".format(TURSO_AUTH_TOKEN),
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        # HTTP错误(如401认证失败/400参数错误), 读取响应体里的真实原因
        try:
            err_body = e.read().decode("utf-8")
        except Exception:
            err_body = "(无法读取错误详情)"
        raise RuntimeError(
            "Turso API返回HTTP错误 {}: {}".format(e.code, err_body[:300]))
    except Exception as e:
        raise RuntimeError("Turso数据库连接失败: {}".format(e))
    # Turso pipeline接口返回格式:
    #   {"baton":..., "base_url":..., "results":[{"type":"ok",
    #     "response":{"type":"execute","result":{"cols":[...],"rows":[...]}}}, ...]}
    # 出错时 results里的type为"error"或整体为{"error":{...}}
    if isinstance(data, dict) and "error" in data:
        raise RuntimeError("Turso API返回错误: {}".format(
            json.dumps(data["error"], ensure_ascii=False)[:300]))
    responses = data.get("results", []) if isinstance(data, dict) else []
    if not responses:
        raise RuntimeError("Turso API返回了意外的数据格式: {}".format(str(data)[:300]))
    # 逐个检查是否有error类型的响应
    for r in responses:
        if isinstance(r, dict) and r.get("type") == "error":
            raise RuntimeError("Turso SQL执行错误: {}".format(
                json.dumps(r.get("error", r), ensure_ascii=False)[:300]))
    # 取第一条execute的结果
    exec_result = responses[0].get("response", {}).get("result", {})
    cols = [c["name"] for c in exec_result.get("cols", [])]
    rows = [[_from_turso_value(v) for v in row] for row in exec_result.get("rows", [])]
    return cols, rows


def _rows_to_dicts(cols, rows):
    """列名+行数据 → 字典列表"""
    return [dict(zip(cols, row)) for row in rows]


def _invalidate():
    """写操作后清空数据缓存。

    load_complaints/get_all_feedbacks 都加了 ttl 缓存提升性能, 但增删改之后
    必须立刻失效, 否则页面会继续显示旧数据。所有写函数统一在这里调一次。
    (只清 cache_data, 不动 cache_resource, 数据库连接/建表状态不受影响)"""
    try:
        st.cache_data.clear()
    except Exception:
        pass   # 脱离Streamlit环境(如跑自检脚本)时忽略


# ------------------------------------------------------------
# 建表
# ------------------------------------------------------------

_COMPLAINTS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS complaints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    complaint_no TEXT UNIQUE,
    customer_name TEXT NOT NULL,
    product_model TEXT,
    problem_type TEXT,
    problem_description TEXT,
    received_date TEXT,
    analysis_deadline TEXT,
    root_cause TEXT,
    action_description TEXT,
    action_date TEXT,
    feedback1_due TEXT,
    feedback2_due TEXT,
    status TEXT DEFAULT '待分析',
    closed_at TEXT,
    created_at TEXT,
    updated_at TEXT
)
"""

_FEEDBACKS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS feedbacks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    complaint_id INTEGER NOT NULL,
    feedback_month INTEGER,
    feedback_date TEXT,
    is_effective TEXT,
    has_similar_complaint TEXT,
    feedback_comment TEXT,
    created_at TEXT
)
"""

# 账号表(本次新增, 不影响任何现有表)
#   role     : 'engineer' / 'manager'
#   is_active: 1启用 / 0停用(停用不删记录, 保留审计痕迹)
#   last_login_at: 为空 = 初始化后从未登录, 兼作"首次登录"标记
_ACCOUNTS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    display_name TEXT,
    role TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT,
    last_login_at TEXT
)
"""

# 客诉责任人列(本次增量新增): 只加列, 不改任何现有列、不动存量数据
#   NULL = 历史数据, 全员可见(初始化前已存在的客诉)
_OWNER_COLUMN = "owner_id"


def _table_columns(table):
    """取表的列名列表(用 PRAGMA, 不依赖 information_schema)"""
    cols, rows = _execute("PRAGMA table_info({})".format(table))
    # PRAGMA table_info 返回: cid, name, type, notnull, dflt_value, pk
    return [r[1] for r in rows]


def ensure_owner_column():
    """给 complaints 表补 owner_id 列(幂等, 已存在则直接跳过)。
    这是纯增量操作: 不修改任何现有列、不迁移数据、不影响现有SQL。"""
    if _OWNER_COLUMN in _table_columns("complaints"):
        return False
    _execute("ALTER TABLE complaints ADD COLUMN {} INTEGER".format(_OWNER_COLUMN))
    _invalidate()
    return True


@st.cache_resource
def init_db():
    """建表(每个进程只执行一次)。"""
    _execute(_COMPLAINTS_TABLE_SQL)
    _execute(_FEEDBACKS_TABLE_SQL)
    _execute(_ACCOUNTS_TABLE_SQL)
    ensure_owner_column()   # 补责任人列(老库升级用, 已有则跳过)
    return True


# ------------------------------------------------------------
# 账号 / 密码 / 登录(本次新增)
# ------------------------------------------------------------
# 说明: 全部用 Python 标准库实现(hashlib + hmac + secrets),
#       不引入 bcrypt/passlib 等第三方包, requirements.txt 不用改。

def hash_password(password, iterations=None, salt_hex=None):
    """生成密码哈希。
    存储格式(单字段自描述, 方便以后换算法或提高迭代次数):
        pbkdf2_sha256$<迭代次数>$<盐hex>$<哈希hex>
    salt_hex 一般不给, 由 secrets 生成每账号独立的随机盐。"""
    iterations = iterations or PASSWORD_ITERATIONS
    salt = bytes.fromhex(salt_hex) if salt_hex else secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, iterations)
    return "pbkdf2_sha256${}${}${}".format(iterations, salt.hex(), dk.hex())


def verify_password(password, stored_hash):
    """校验密码。用 hmac.compare_digest 做定长时间比较, 防时序攻击。
    任何异常(格式错/字段空)一律返回False, 不抛异常。"""
    if not password or not stored_hash:
        return False
    try:
        algo, iters, salt_hex, hash_hex = str(stored_hash).split("$")
        if algo != "pbkdf2_sha256":
            return False
        calc = hash_password(password, int(iters), salt_hex)
        return hmac.compare_digest(calc.split("$")[3], hash_hex)
    except Exception:
        return False


def validate_password(password, username=None):
    """密码策略校验: 长度 >= PASSWORD_MIN_LEN, 且同时含字母和数字, 且不等于用户名。
    返回 (是否合规, 不合规时的提示语)。"""
    if not password or len(password) < PASSWORD_MIN_LEN:
        return False, "密码长度不能少于 {} 位".format(PASSWORD_MIN_LEN)
    if not re.search(r"[A-Za-z]", password) or not re.search(r"\d", password):
        return False, "密码必须同时包含字母和数字"
    if username and str(password).lower() == str(username).lower():
        return False, "密码不能与账号相同"
    return True, ""


def gen_random_password(length=10):
    """生成满足密码策略的随机初始密码(必须含字母和数字)。
    用于 init_accounts.py, 生成后只在终端打印一次。"""
    alphabet = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    while True:
        pwd = "".join(secrets.choice(alphabet) for _ in range(length))
        ok, _ = validate_password(pwd)
        if ok:
            return pwd


def get_account(username):
    """按登录账号取账号记录(含 password_hash, 仅认证内部使用, 不要放进session)"""
    cols, rows = _execute("SELECT * FROM accounts WHERE username = ?", [username])
    ds = _rows_to_dicts(cols, rows)
    return ds[0] if ds else None


def get_account_by_id(account_id):
    cols, rows = _execute("SELECT * FROM accounts WHERE id = ?", [account_id])
    ds = _rows_to_dicts(cols, rows)
    return ds[0] if ds else None


def list_accounts():
    """账号列表(不返回 password_hash, 避免密码哈希泄漏到页面)"""
    cols, rows = _execute(
        "SELECT id, username, display_name, role, is_active, created_at, "
        "last_login_at FROM accounts ORDER BY id ASC")
    return _rows_to_dicts(cols, rows)


@st.cache_data(ttl=60)
def get_account_names():
    """账号id → 姓名的映射, 供列表页展示"责任人"用(不查密码字段)"""
    result = {}
    for a in list_accounts():
        result[a["id"]] = a.get("display_name") or a.get("username") or str(a["id"])
    return result


def create_account(username, password, display_name, role, is_active=1):
    """创建账号。已存在则跳过(不覆盖、不删除任何已有数据)。
    返回 (是否新建成功, 提示语)。"""
    if role not in ROLE_CODES:
        raise ValueError("角色只能是 {} 之一".format("/".join(ROLE_CODES)))
    ok, msg = validate_password(password, username)
    if not ok:
        raise ValueError(msg)
    if get_account(username):
        return False, "账号 {} 已存在，已跳过（未做任何修改）".format(username)
    _execute(
        "INSERT INTO accounts (username, password_hash, display_name, role, "
        "is_active, created_at) VALUES (?,?,?,?,?,?)",
        [username, hash_password(password), display_name, role,
         1 if is_active else 0, _now_str()])
    _invalidate()
    return True, "账号 {} 创建成功".format(username)


def update_password(account_id, new_password, username=None):
    """修改密码(同样走密码策略校验)"""
    ok, msg = validate_password(new_password, username)
    if not ok:
        raise ValueError(msg)
    _execute("UPDATE accounts SET password_hash = ? WHERE id = ?",
             [hash_password(new_password), account_id])
    _invalidate()


def authenticate(username, password):
    """登录校验。
    返回 (是否成功, 账号dict 或 None, 失败原因)
      - 账号不存在 / 已停用 / 密码错误 —— 都返回失败, 由页面统一提示
        "账号或密码错误", 不区分具体原因, 防止被拿来枚举账号
      - 成功时返回的账号dict 已剔除 password_hash, 可安全放进 session
      - 成功时顺带写入 last_login_at, 并用"登录前是否为空"判断是否首次登录
    """
    acc = get_account(username)
    if not acc:
        return False, None, "账号不存在"
    if not acc.get("is_active"):
        return False, None, "账号已停用"
    if not verify_password(password, acc.get("password_hash")):
        return False, None, "密码错误"

    is_first_login = not acc.get("last_login_at")
    _execute("UPDATE accounts SET last_login_at = ? WHERE id = ?",
             [_now_str(), acc["id"]])
    _invalidate()

    acc.pop("password_hash", None)      # 密码哈希绝不进 session
    acc["is_first_login"] = is_first_login
    return True, acc, ""


# ------------------------------------------------------------
# 角色 / 数据权限(本次新增)
# ------------------------------------------------------------

def role_code_of(user):
    """取用户的角色码('engineer'/'manager'), 兼容传入中文角色的情况。"""
    if not user:
        return None
    code = user.get("role_code") or user.get("role")
    if code in ROLE_CODES:
        return code
    return ROLE_CN_TO_CODE.get(code)


def role_cn_of(user):
    """取用户的中文角色名(页面展示用)"""
    return ROLE_CODE_TO_CN.get(role_code_of(user)) or "-"


def is_manager(user):
    return role_code_of(user) == ROLE_CODE_MANAGER


def can_access(complaint, user):
    """判断 user 能否查看/操作该客诉 —— 数据隔离的唯一判定入口。
    规则:
      1) 经理: 全部可见
      2) 工程师: 只能看 owner_id 等于自己 的
      3) owner_id 为 NULL 的历史数据: 全员可见(初始化前的老客诉)"""
    if not user:
        return True          # 未登录/自检场景不做限制
    if is_manager(user):
        return True
    owner = complaint.get(_OWNER_COLUMN)
    if owner is None:        # 老数据没有责任人, 全员可见
        return True
    return owner == user.get("id")


def assert_owner(complaint_id, user, action="操作"):
    """写操作前的归属校验(第二层防护): 不是自己的客诉直接抛异常。
    即使有人绕过页面直接调函数也过不了这一关。"""
    c = get_complaint(complaint_id)
    if not c:
        raise RuntimeError("客诉不存在: {}".format(complaint_id))
    if not can_access(c, user):
        raise RuntimeError("无权{}该客诉（{}）：该客诉不属于当前账号".format(
            action, c.get("complaint_no")))
    return c


def assert_manager(user, action="执行该操作"):
    """经理专属操作的角色校验(第三层防护, 放在db层而不是只在UI藏按钮)"""
    if not is_manager(user):
        raise RuntimeError("只有经理可以{}".format(action))


# ------------------------------------------------------------
# 客诉主表操作
# ------------------------------------------------------------

def _next_complaint_no():
    """生成下一个客诉编号: TS-年份-4位序号, 如 TS-2026-0001。
    查当年最大编号+1(编号零填充4位, 字符串排序可靠)。"""
    year = date.today().year
    prefix = "TS-{}-".format(year)
    cols, rows = _execute(
        "SELECT complaint_no FROM complaints WHERE complaint_no LIKE ? "
        "ORDER BY complaint_no DESC LIMIT 1",
        [prefix + "%"],
    )
    if rows and rows[0][0]:
        last_seq = int(str(rows[0][0]).split("-")[-1])
    else:
        last_seq = 0
    return "{}{:04d}".format(prefix, last_seq + 1)


def analysis_deadline_of(received_date_str):
    """分析截止日期 = 收货日期 + ANALYSIS_DAYS 个自然日(供录入页回显)"""
    return (date.fromisoformat(str(received_date_str))
            + timedelta(days=ANALYSIS_DAYS)).isoformat()


def insert_complaint(customer_name, product_model, problem_type,
                     problem_description, received_date, owner_id=None):
    """工程师录入新客诉。
    自动: 生成编号 / 算分析截止(收货+N个自然日) / 状态=待分析
          / 记录责任人 owner_id = 当前登录人(数据隔离用)。"""
    complaint_no = _next_complaint_no()
    received = date.fromisoformat(str(received_date))
    analysis_deadline = (received + timedelta(days=ANALYSIS_DAYS)).isoformat()
    now = _now_str()
    _execute(
        "INSERT INTO complaints (complaint_no, customer_name, product_model, "
        "problem_type, problem_description, received_date, analysis_deadline, "
        "status, owner_id, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [complaint_no, customer_name, product_model, problem_type,
         problem_description, str(received_date), analysis_deadline,
         STATUS_ANALYSIS, owner_id, now, now],
    )
    _invalidate()
    return complaint_no


# 允许的排序方式白名单(拼进ORDER BY, 必须白名单校验防止SQL注入)
_ORDER_SQL = {
    "id_asc": "id ASC",
    "id_desc": "id DESC",
    "closed_desc": "closed_at DESC, id DESC",
    "received_desc": "received_date DESC, id DESC",
}


@st.cache_data(ttl=30)
def _load_complaints_cached(status, order, scope, scope_id):
    """真正查库的函数(只接收可哈希参数, 才能用 st.cache_data)。
    scope='all'  : 不过滤(经理 / 未登录场景)
    scope='owner': 只看 scope_id 这个责任人的 + owner_id为NULL的历史数据(工程师)

    数据隔离在 SQL 层就完成 —— 工程师是"查不到", 不是"查出来再藏起来"。"""
    order_sql = _ORDER_SQL.get(order, _ORDER_SQL["id_asc"])
    sql = "SELECT * FROM complaints"
    where, args = [], []
    if status:
        where.append("status = ?")
        args.append(status)
    if scope == "owner":
        where.append("({} = ? OR {} IS NULL)".format(_OWNER_COLUMN, _OWNER_COLUMN))
        args.append(scope_id)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY " + order_sql
    cols, rows = _execute(sql, args)
    return _rows_to_dicts(cols, rows)


def load_complaints(status=None, order="id_asc", viewer=None):
    """加载客诉列表(带数据权限过滤)。
    status : 传具体状态则筛选, 传None返回全部
    order  : 排序方式(见_ORDER_SQL白名单), 默认按id升序
    viewer : 当前登录用户dict; 工程师自动只取自己的数据, 经理取全部
    加ttl缓存: Streamlit每次交互都会重跑脚本, 30秒内复用结果, 减少对Turso的请求数。
    写操作后由 db._invalidate() 统一清空, 不会出现数据不新鲜。"""
    if viewer is None or is_manager(viewer):
        scope, scope_id = "all", None
    else:
        scope, scope_id = "owner", viewer.get("id")
    return _load_complaints_cached(status, order, scope, scope_id)


def load_closed(viewer=None):
    """已关闭列表: 按关闭时间倒序(最近关闭的排最前)。"""
    return load_complaints(STATUS_CLOSED, order="closed_desc", viewer=viewer)


def get_complaint(complaint_id):
    """取单条客诉"""
    cols, rows = _execute(
        "SELECT * FROM complaints WHERE id = ?", [complaint_id])
    ds = _rows_to_dicts(cols, rows)
    return ds[0] if ds else None


def _void_round_feedbacks(complaint_id):
    """作废该客诉"当前跟踪期"的全部反馈记录(把feedback_month置为负数)。

    为什么需要: 本轮反馈靠 feedback_date >= action_date 来识别。若工程师在退回
    (或反馈不达标重改)当天就重新提交措施, 新的 action_date 会等于上轮最后一次
    反馈的日期, 上轮那条反馈就会被误判进新的一轮, 导致新跟踪期直接跳过第1次反馈。

    做法: 不删记录(历史要留痕), 只把 feedback_month 取负, 查询本轮时加
    feedback_month > 0 过滤。用 -ABS() 保证重复调用不会把负数再翻回正数。
    """
    _execute(
        "UPDATE feedbacks SET feedback_month = -ABS(feedback_month) "
        "WHERE complaint_id = ?",
        [complaint_id],
    )


def submit_analysis_action(complaint_id, root_cause, action_description,
                           action_date=None, viewer=None):
    """提交根因结论+改善措施。
    viewer: 当前登录用户, 提交前会校验归属(不是自己的客诉直接拒绝)。
    自动: 记录action_date / 算两次反馈截止 / 状态→跟踪中。
    从"待分析"或"待措施"提交都走这里, 重新整改时跟踪期自动重新开始:
      1) 先作废上一轮反馈(见_void_round_feedbacks)
      2) 再覆盖 action_date 与两次反馈截止日期

    action_date: 措施完成时间, 默认今天。调试模式下由页面传入, 便于验证提醒规则。
    """
    assert_owner(complaint_id, viewer, "制定措施")   # 归属校验
    action_date = action_date or _today_str()
    d = date.fromisoformat(str(action_date))
    feedback1_due = add_months(d, 1).isoformat()   # 第1次反馈 = 措施日期+1个月
    feedback2_due = add_months(d, 2).isoformat()   # 第2次反馈 = 措施日期+2个月
    now = _now_str()
    _void_round_feedbacks(complaint_id)
    _execute(
        "UPDATE complaints SET root_cause = ?, action_description = ?, "
        "action_date = ?, feedback1_due = ?, feedback2_due = ?, "
        "status = ?, updated_at = ? WHERE id = ?",
        [root_cause, action_description, action_date,
         feedback1_due, feedback2_due, STATUS_TRACKING, now, complaint_id],
    )
    _invalidate()
    return action_date, feedback1_due, feedback2_due


# ------------------------------------------------------------
# 措施跟踪反馈操作
# ------------------------------------------------------------

@st.cache_data(ttl=30)
def get_all_feedbacks():
    """一次性取出全部反馈记录, 按 complaint_id 分组返回 {id: [反馈,...]}。

    性能优化的关键: 以前列表页/提醒区每显示一条客诉就查一次库(N+1次远程请求),
    现在整张反馈表只查一次, 之后全在内存里过滤, 页面切换不再打库。
    每条反馈额外算出 is_void 字段: True=上一轮已作废的记录(只留痕, 不参与判定)。
    """
    cols, rows = _execute("SELECT * FROM feedbacks ORDER BY id ASC")
    grouped = {}
    for fb in _rows_to_dicts(cols, rows):
        # feedback_month 被置负 = 该反馈所属跟踪期已作废(经理退回/反馈未达标重改)
        fb["is_void"] = (fb.get("feedback_month") or 0) < 0
        grouped.setdefault(fb.get("complaint_id"), []).append(fb)
    return grouped


def round_feedbacks(complaint, fb_map=None):
    """取某条客诉【当前跟踪期】的有效反馈(按提交顺序返回)。
    判定条件(两个都要满足, 缺一不可):
      1) feedback_month > 0 —— 排除上一轮作废的记录(Bug2修复)
      2) feedback_date >= action_date —— 属于本轮措施之后提交的
    fb_map: 已经取好的分组数据, 传进来可避免重复查库; 不传则自己取一次。"""
    if fb_map is None:
        fb_map = get_all_feedbacks()
    action_date = complaint.get("action_date")
    if not action_date:
        return []
    fbs = fb_map.get(complaint.get("id"), [])
    return [fb for fb in fbs
            if not fb["is_void"] and (fb.get("feedback_date") or "") >= action_date]


def get_feedback_history(complaint_id, fb_map=None):
    """取某条客诉的【全部】反馈记录(含已作废轮次), 供详情页展示整改历史。"""
    if fb_map is None:
        fb_map = get_all_feedbacks()
    return list(fb_map.get(complaint_id, []))


def get_feedbacks_after_action(complaint_id, fb_map=None):
    """取当前跟踪期的反馈记录(兼容旧调用方式: 只传客诉id)。"""
    c = get_complaint(complaint_id)
    if not c:
        return []
    return round_feedbacks(c, fb_map)


def submit_feedback(complaint_id, is_effective, has_similar_complaint, comment,
                    viewer=None):
    """提交跟踪反馈。
    viewer: 当前登录用户, 提交前会校验归属(不是自己的客诉直接拒绝)。
    反馈序号 = 当前跟踪期已有有效反馈数 + 1(第1次或第2次)。
    提交后按统一规则流转状态(规则见 decide_status_after_feedback):
      - 任一次反馈不合格(有效=否 或 同类=是) → 待措施
        其中第1次就不合格的(v0.4): 立即作废本轮反馈, 不再等第2次
      - 轮末达标(第2次) → 待关闭(等经理确认)
    返回(本次序号, 是否达标): 达标=True / 不合格=False /
    第1次反馈合格时为None(不判定, 等第2次)。"""
    c = assert_owner(complaint_id, viewer, "提交反馈")   # 归属校验
    existing = round_feedbacks(c)
    month = len(existing) + 1
    if month > FEEDBACK_ROUNDS:
        raise RuntimeError(
            "当前跟踪期{}次反馈均已提交, 请等待经理处理或重新制定措施".format(
                FEEDBACK_ROUNDS))
    feedback_date = _today_str()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _execute(
        "INSERT INTO feedbacks (complaint_id, feedback_month, feedback_date, "
        "is_effective, has_similar_complaint, feedback_comment, created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        [complaint_id, month, feedback_date, is_effective,
         has_similar_complaint, comment, now],
    )
    # 先清缓存再判定: 否则 round_feedbacks 读到的还是插入前的旧数据,
    # 会把刚提交的这条漏掉, 导致"两次达标"永远判定为不达标。
    _invalidate()
    # 统一判定: 提交本次反馈后应流转到哪个状态
    # (v0.4起第1次反馈不合格也立即判定, 不再只判定最后一次)
    all_fb = round_feedbacks(c)
    new_status = decide_status_after_feedback(month, all_fb)
    qualified = None   # 第1次反馈合格时不做整轮判定, 返回None
    if new_status != STATUS_TRACKING:
        # 状态要流转: 达标=True(转待关闭), 不合格=False(转待措施)
        qualified = is_round_qualified(all_fb)
        if month == 1:
            # v0.4新规则: 第1次反馈就不合格, 立即作废本轮反馈(置负留痕),
            # 不等第2次; 工程师重新提交措施后跟踪期重新开始
            _void_round_feedbacks(complaint_id)
        _execute(
            "UPDATE complaints SET status = ?, updated_at = ? WHERE id = ?",
            [new_status, now, complaint_id],
        )
        _invalidate()   # 状态也变了, 再清一次
    return month, qualified


# ------------------------------------------------------------
# 经理关闭/退回
# ------------------------------------------------------------

def close_complaint(complaint_id, viewer=None):
    """经理确认关闭: 状态→已关闭, 记录关闭时间。
    两道校验: 1) 只有经理能关  2) 本轮达标才能关(防止异常数据被误关闭)。"""
    assert_manager(viewer, "确认关闭客诉")
    c = get_complaint(complaint_id)
    if not c:
        raise RuntimeError("客诉不存在: {}".format(complaint_id))
    fbs = round_feedbacks(c)
    if not is_round_qualified(fbs):
        raise RuntimeError(
            "该客诉本轮只有 {} 条达标反馈(需 {} 条且均为'措施有效=是/无同类投诉'), "
            "不能关闭。请先完成跟踪反馈或退回重新整改。".format(
                len(fbs), FEEDBACK_ROUNDS))
    now = _now_str()
    _execute(
        "UPDATE complaints SET status = ?, closed_at = ?, updated_at = ? "
        "WHERE id = ?",
        [STATUS_CLOSED, now, now, complaint_id],
    )
    _invalidate()


def reopen_to_action(complaint_id, viewer=None):
    """经理退回: 状态→待措施, 工程师重新制定措施后跟踪期重新开始。
    同时作废本轮反馈(置负), 避免旧反馈被算进下一轮(Bug2修复)。
    历史记录一条不删, 详情页仍可查看整改进过。"""
    assert_manager(viewer, "退回客诉")
    now = _now_str()
    _void_round_feedbacks(complaint_id)
    _execute(
        "UPDATE complaints SET status = ?, updated_at = ? WHERE id = ?",
        [STATUS_ACTION, now, complaint_id],
    )
    _invalidate()


def delete_complaint(complaint_id, viewer=None):
    """删除客诉(带关联清理 + 归属校验):
    先删 feedbacks 表中该客诉的全部反馈记录(避免孤儿数据),
    再删 complaints 主表记录。两步都成功才算删除完成。"""
    assert_owner(complaint_id, viewer, "删除")   # 归属校验
    _execute("DELETE FROM feedbacks WHERE complaint_id = ?", [complaint_id])
    _execute("DELETE FROM complaints WHERE id = ?", [complaint_id])
    _invalidate()


# ------------------------------------------------------------
# 提醒区数据
# ------------------------------------------------------------

def _push_deadline(result, complaint, due_str, key, today):
    """把一条客诉按截止日期归入"逾期/即将到期"桶(内部小工具)。
    key 形如 'analysis_' / 'fb1_' / 'fb2_', 最终落到 key+'overdue' 或 key+'soon'。"""
    level = classify_deadline(due_str, today)
    if level == "overdue":
        result[key + "overdue"].append(complaint)
    elif level == "soon":
        result[key + "soon"].append(complaint)


def get_alerts(today=None, viewer=None):
    """汇总提醒数据(系统内提醒, 代替人工催办)。
    viewer: 当前登录用户; 工程师只统计自己负责的客诉, 经理统计全部。

    性能说明: 全程只查2次库(客诉全表 + 反馈全表), 其余在内存里算。
    改造前每条"跟踪中"客诉都要单独查一次反馈(N+1次远程请求), 数据量一大就卡。

    返回字典(每个值都是客诉字典列表):
      分析:      analysis_overdue(红) / analysis_soon(橙)
      第1次反馈: fb1_overdue / fb1_soon
      第2次反馈: fb2_overdue / fb2_soon
      其他:      pending_close(待经理关闭) / need_action(待重新制定措施)
                 abnormal(兜底: 跟踪中但反馈已交满, 正常流程不会出现)
    """
    today = today or date.today()
    fb_map = get_all_feedbacks()          # 反馈全表只查一次
    result = {
        "analysis_overdue": [], "analysis_soon": [],
        "fb1_overdue": [], "fb1_soon": [],
        "fb2_overdue": [], "fb2_soon": [],
        "pending_close": [],
        "need_action": [],   # 待措施: 需重新制定措施(无截止日期, 只点名不催办)
        "abnormal": [],      # 兜底提醒, 防止客诉停在"跟踪中"无人处理
    }
    for c in load_complaints(viewer=viewer):   # 客诉全表只查一次(已按权限过滤)
        status = c.get("status")
        if status == STATUS_ANALYSIS:
            _push_deadline(result, c, c.get("analysis_deadline"), "analysis_", today)
        elif status == STATUS_TRACKING:
            done = len(round_feedbacks(c, fb_map))
            if done >= FEEDBACK_ROUNDS:
                result["abnormal"].append(c)   # 正常流程不会停在这里
                continue
            # 已交0条 → 催第1次反馈; 已交1条 → 催第2次反馈
            key = "fb1_" if done == 0 else "fb2_"
            due = c.get("feedback1_due") if done == 0 else c.get("feedback2_due")
            _push_deadline(result, c, due, key, today)
        elif status == STATUS_PENDING_CLOSE:
            result["pending_close"].append(c)
        elif status == STATUS_ACTION:
            # 待措施要等重新提交措施后才有新的截止日期, 这里只点名不催办
            result["need_action"].append(c)
    return result


def build_task_list(alerts, fb_map=None, today=None, viewer=None):
    """把提醒数据整理成「待处理任务」清单, 按紧急度排序。

    覆盖范围 = 该用户所有"还没闭环"的客诉:
      1) 逾期(红)  2) 即将到期(橙)  3) 需重新制定措施  4) 其他未到期但有下一步动作的
    这样"我的待处理任务"这个总数, 才等于后面几张提醒卡的汇总, 不会出现数字对不上。

    每条任务: {level, action, complaint, due, days_left, task_label}
      level : overdue(逾期,红) > soon(即将到期,橙) > todo(待办,蓝)
      action: analysis(填根因措施) / feedback(提交第N次反馈) / reopen(重新制定措施)
    """
    if fb_map is None:
        fb_map = get_all_feedbacks()
    today = today or date.today()
    tasks = []

    def _add(level, action, c, due=None):
        # days_left: 距截止还剩几天(负数=已逾期多少天); 没有截止日期则为None
        days_left = None
        if due:
            try:
                days_left = (date.fromisoformat(str(due)[:10]) - today).days
            except (ValueError, TypeError):
                days_left = None
        # 反馈类任务要算出"这是第几次反馈", 页面提示要用
        feedback_no = None
        if action == "feedback":
            feedback_no = len(round_feedbacks(c, fb_map)) + 1
        tasks.append({
            "level": level, "action": action, "complaint": c,
            "due": due, "days_left": days_left, "feedback_no": feedback_no,
        })

    # ---- 1) 逾期任务(红) ----
    for key, action, is_fb1 in (("analysis_overdue", "analysis", None),
                                ("fb1_overdue", "feedback", True),
                                ("fb2_overdue", "feedback", False)):
        for c in alerts.get(key, []):
            due = (c.get("analysis_deadline") if action == "analysis"
                   else (c.get("feedback1_due") if is_fb1 else c.get("feedback2_due")))
            _add("overdue", action, c, due)

    # ---- 2) 即将到期(橙) ----
    for key, action, is_fb1 in (("analysis_soon", "analysis", None),
                                ("fb1_soon", "feedback", True),
                                ("fb2_soon", "feedback", False)):
        for c in alerts.get(key, []):
            due = (c.get("analysis_deadline") if action == "analysis"
                   else (c.get("feedback1_due") if is_fb1 else c.get("feedback2_due")))
            _add("soon", action, c, due)

    # ---- 3) 需要重新制定措施(待措施): 无截止日期, 归为普通待办 ----
    for c in alerts.get("need_action", []):
        _add("todo", "reopen", c, None)

    # ---- 4) 其他待办: 还没逾期也没快到期的 待分析/跟踪中, 同样要处理 ----
    #      (没有这一步, "待处理任务"的总数会比实际少一截)
    flagged = set()
    for key in ("analysis_overdue", "analysis_soon", "fb1_overdue", "fb1_soon",
                "fb2_overdue", "fb2_soon", "need_action"):
        for c in alerts.get(key, []):
            flagged.add(c.get("id"))
    for c in load_complaints(viewer=viewer):
        if c.get("id") in flagged:
            continue
        if c.get("status") == STATUS_ANALYSIS:
            _add("todo", "analysis", c, c.get("analysis_deadline"))
        elif c.get("status") == STATUS_TRACKING:
            done = len(round_feedbacks(c, fb_map))
            due = c.get("feedback1_due") if done == 0 else c.get("feedback2_due")
            _add("todo", "feedback", c, due)

    # 排序: 逾期(逾期越久越靠前) → 即将到期(越快到期越靠前) → 待办
    level_rank = {"overdue": 0, "soon": 1, "todo": 2}
    tasks.sort(key=lambda t: (level_rank[t["level"]],
                              t["days_left"] if t["days_left"] is not None else 0,
                              t["complaint"].get("id") or 0))
    return tasks


# ------------------------------------------------------------
# 分析看板数据
# ------------------------------------------------------------

# 允许分组统计的字段白名单(字段名要拼进SQL, 必须校验防止SQL注入)
_COUNTABLE_FIELDS = {"problem_type", "status", "customer_name", "product_model"}


def count_by(field):
    """按指定字段分组计数, 供看板用。字段必须在白名单内。"""
    if field not in _COUNTABLE_FIELDS:
        raise ValueError("不允许按该字段统计: {}".format(field))
    cols, rows = _execute(
        "SELECT {}, COUNT(*) FROM complaints GROUP BY {} ORDER BY 2 DESC".format(
            field, field))
    return {r[0]: r[1] for r in rows if r[0]}


def count_closed_by_month(months=12):
    """关闭趋势数据: 最近 months 个月每月关闭了多少条客诉。
    closed_at 形如 '2026-08-30 14:20:01', 取前7位即 'YYYY-MM' 作为月份。
    没有数据的月份补0, 保证趋势图横坐标连续(不会跳月)。
    返回 [(月份字符串, 数量), ...], 按月份升序。"""
    cols, rows = _execute(
        "SELECT substr(closed_at, 1, 7) AS ym, COUNT(*) FROM complaints "
        "WHERE closed_at IS NOT NULL AND closed_at <> '' "
        "GROUP BY ym ORDER BY ym ASC")
    data = {str(r[0]): r[1] for r in rows if r[0]}
    today = date.today()
    month_keys = []
    for i in range(months - 1, -1, -1):
        d = add_months(date(today.year, today.month, 1), -i)
        month_keys.append("{:04d}-{:02d}".format(d.year, d.month))
    return [(k, data.get(k, 0)) for k in month_keys]


def close_summary():
    """关闭看板的三个指标: 累计关闭数 / 本月关闭数 / 平均闭环天数(收货→关闭)。
    平均闭环天数 = 关闭日期 - 收货日期, 只统计两个日期都存在的记录。"""
    closed = load_closed()
    month_key = date.today().strftime("%Y-%m")
    this_month = 0
    day_spans = []
    for c in closed:
        closed_at = str(c.get("closed_at") or "")
        received = str(c.get("received_date") or "")
        if closed_at.startswith(month_key):
            this_month += 1
        if len(closed_at) >= 10 and len(received) >= 10:
            try:
                d1 = date.fromisoformat(received[:10])
                d2 = date.fromisoformat(closed_at[:10])
                day_spans.append((d2 - d1).days)
            except ValueError:
                continue   # 脏数据跳过, 不影响整体统计
    avg_days = round(sum(day_spans) / len(day_spans), 1) if day_spans else 0
    return {
        "total": len(closed),
        "this_month": this_month,
        "avg_days": avg_days,
    }


# ------------------------------------------------------------
# 一次整改成功率(重开率)
# ------------------------------------------------------------

def first_pass_stats(fb_map=None):
    """一次整改成功率统计 —— 衡量"措施一次就管用"的比例。

    口径(与看板其他指标一样, 全部基于现有字段, 不加表不加列):
      已进入跟踪期的客诉数(分母) = complaints 里 action_date 非空的客诉条数
        —— 只要制定过措施、进入过2个月跟踪期, 就计入分母
      重开过的客诉数(分子)     = 存在 feedback_month < 0 反馈记录的客诉条数
        —— feedback_month 被置负 = 上一轮反馈作废, 即"这一轮没一次通过"
          (触发来源: 经理退回重改、第2次反馈未达标自动转待措施, 见
           _void_round_feedbacks / decide_status_after_feedback)

      重开率     = 分子 / 分母 × 100%
      一次通过率 = 100% - 重开率

    注意:
      1) 分子按"客诉条数"去重, 一条客诉重开3次也只算1次, 不会让重开率超过100%
      2) 分子只统计仍然存在的主表记录, 避免脏数据导致分子大于分母
      3) 性能: 复用已缓存的 load_complaints / get_all_feedbacks, 不额外查库

    返回: {total, reopened, reopen_rate, pass_rate, has_data}
      has_data=False 表示还没有任何客诉进入跟踪期, 页面应显示"暂无数据"
    """
    if fb_map is None:
        fb_map = get_all_feedbacks()
    complaints = load_complaints()

    # 分母: 已进入跟踪期的客诉(action_date 非空)
    tracked = [c for c in complaints if c.get("action_date")]

    # 先收集"有作废反馈"的客诉id集合
    reopened_ids = set()
    for cid, fb_list in fb_map.items():
        for fb in fb_list:
            if (fb.get("feedback_month") or 0) < 0:
                reopened_ids.add(cid)

    # 分子: 在分母范围内、且有过作废反馈的客诉条数
    reopened = sum(1 for c in tracked if c.get("id") in reopened_ids)

    total = len(tracked)
    if total == 0:
        return {"total": 0, "reopened": 0, "reopen_rate": 0.0,
                "pass_rate": 0.0, "has_data": False}
    reopen_rate = round(reopened * 100.0 / total, 1)
    return {
        "total": total,
        "reopened": reopened,
        "reopen_rate": reopen_rate,
        "pass_rate": round(100.0 - reopen_rate, 1),
        "has_data": True,
    }


# ------------------------------------------------------------
# 反馈提交时效(按时 / 逾期)
# ------------------------------------------------------------

def feedback_timeliness(feedback, complaint):
    """判定一条反馈是"按时提交"还是"逾期提交"。

    判断: 用反馈日期 减去 该次反馈对应的截止日期
      > 0 天 → 逾期, 返回 ("逾期X天提交", "late")  页面标红
      <= 0天 → 按时, 返回 ("按时提交", "ok")       页面标绿

    该次反馈的截止日期取法: feedback_month 绝对值
      1 → complaint.feedback1_due
      2 → complaint.feedback2_due

    特殊情况返回 ("历史轮次", "unknown") 置灰:
      已作废的反馈(feedback_month < 0)属于上一轮, 而上一轮的截止日期在新措施
      提交时已被覆盖写入, 无法还原, 不做无依据的判断。
    """
    if feedback.get("is_void"):
        return "历史轮次", "unknown"

    month = abs(feedback.get("feedback_month") or 0)
    due = (complaint.get("feedback1_due") if month == 1
           else complaint.get("feedback2_due"))
    fdate = feedback.get("feedback_date")
    if not due or not fdate:
        return "-", "unknown"
    try:
        late_days = (date.fromisoformat(str(fdate)[:10])
                     - date.fromisoformat(str(due)[:10])).days
    except (ValueError, TypeError):
        return "-", "unknown"
    if late_days > 0:
        return "逾期{}天提交".format(late_days), "late"
    return "按时提交", "ok"


# ------------------------------------------------------------
# 同类问题重复发生检查
# ------------------------------------------------------------

def find_similar_history(customer_name, problem_type, months=None,
                         exclude_id=None, fb_map=None):
    """新增客诉时检查"同客户 + 同问题类型"的历史客诉, 用于预防再发(8D的D7)。

    months    : 回溯窗口, 默认取 config.SIMILAR_CHECK_MONTHS(12个月)
    exclude_id: 要排除的客诉id(编辑/提交后回看时排除自己)
    返回按收货日期倒序的历史客诉列表(可能为空)。

    性能: 复用已缓存的 load_complaints, 不额外查库。
    """
    if not customer_name or not problem_type:
        return []
    months = SIMILAR_CHECK_MONTHS if months is None else months
    # 回溯起算日: 今天往前推 months 个月(用 add_months 处理月末溢出)
    cutoff = add_months(date.today(), -months).isoformat()

    hit = []
    for c in load_complaints():
        if exclude_id is not None and c.get("id") == exclude_id:
            continue
        if c.get("customer_name") != customer_name:
            continue
        if c.get("problem_type") != problem_type:
            continue
        received = str(c.get("received_date") or "")
        if received >= cutoff:      # ISO日期字符串可直接比较大小
            hit.append(c)
    # 最近发生的排最前
    hit.sort(key=lambda x: str(x.get("received_date") or ""), reverse=True)
    return hit
