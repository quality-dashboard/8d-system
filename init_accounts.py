"""
init_accounts.py - 8D系统 账号初始化脚本
------------------------------------------------
用途: 第一次启用登录功能时运行一次, 创建默认的经理和工程师账号。

用法:
    cd D:\\8d_system
    python init_accounts.py

行为(安全优先):
    1. 建 accounts 表(已存在则跳过)
    2. 补 complaints.owner_id 列(已存在则跳过)
    3. 创建两个默认账号 —— 只在账号不存在时插入, 已存在直接跳过并提示,
       **不修改、不删除任何已有数据**(包括已有的客诉/反馈/账号)
    4. 初始密码随机生成(满足: 至少6位且同时含字母和数字), 只在终端打印一次
    5. 账号首次登录时系统会强制要求修改密码

后续新增工程师账号, 可以复制本文件改一下要建的账号再跑, 或者直接调:
    python -c "import db; print(db.create_account('zhangsan', 'Abc123456', '张三', 'engineer'))"
"""
import sys

import db
from config import ROLE_CODE_MANAGER, ROLE_CODE_ENGINEER, ROLE_CODE_TO_CN


def main():
    print("=" * 56)
    print("8D客诉整改跟踪系统 · 账号初始化")
    print("=" * 56)

    # ---- 1) 建表 / 补列(都是幂等操作, 重复跑不会有副作用) ----
    try:
        db.init_db()
    except Exception as e:
        print("[失败] 数据库初始化出错: {}".format(e))
        print("请检查 .streamlit/secrets.toml 里的 TURSO_8D_URL / TURSO_8D_TOKEN")
        sys.exit(1)

    # ---- 2) 创建默认账号(已存在则跳过, 不覆盖) ----
    plans = [
        # (登录账号, 展示姓名, 角色码)
        ("manager", "质量经理", ROLE_CODE_MANAGER),
        ("engineer", "质量工程师", ROLE_CODE_ENGINEER),
    ]
    created = []
    for username, display_name, role in plans:
        # 初始密码随机生成, 只在这里打印一次, 不写进任何文件
        password = db.gen_random_password()
        try:
            ok, msg = db.create_account(username, password, display_name, role)
        except ValueError as e:
            # 生日碰撞极小概率下密码不符合策略(不会发生, 因为gen时已校验), 兜底
            print("[失败] {}: {}".format(username, e))
            sys.exit(1)
        except Exception as e:
            print("[失败] 创建 {} 出错: {}".format(username, e))
            sys.exit(1)

        if ok:
            created.append((username, display_name, role, password))
            print("\n[新建] 账号: {}".format(username))
            print("       姓名: {}".format(display_name))
            print("       角色: {}".format(ROLE_CODE_TO_CN.get(role, role)))
            print("       初始密码: {}   ← 请记好, 只显示这一次!".format(password))
        else:
            print("\n[跳过] {} —— {}".format(username, msg))

    # ---- 3) 现状摘要 ----
    print("\n" + "-" * 56)
    print("当前账号一览(不显示密码):")
    for a in db.list_accounts():
        state = "启用" if a.get("is_active") else "停用"
        last = a.get("last_login_at") or "从未登录"
        print("  {} | {} | {} | {} | 最后登录: {}".format(
            a["username"], a.get("display_name") or "-", ROLE_CODE_TO_CN.get(
                a.get("role"), a.get("role")), state, last))

    print("\n下一步:")
    if created:
        print("  1. 运行 streamlit run app.py 启动系统")
        print("  2. 用上面的账号 + 初始密码登录")
        print("  3. 首次登录会强制要求修改密码, 修改后才能使用")
    else:
        print("  账号均已存在, 无需重新初始化; 忘记密码请联系管理员重置。")
    print("=" * 56)


if __name__ == "__main__":
    main()
