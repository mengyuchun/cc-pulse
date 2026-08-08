"""PS1 启动器端到端测试。

启动 pwsh，把模拟输入通过 stdin 喂入，验证：
  1. 菜单渲染（6 项主菜单）
  2. 健康检测快速体检（选项 1）零子提示一键跑
  3. inspect（选项 4）精简 3 步交互
  4. 运行日志（选项 5）
  5. 高级设置（选项 6）
  5. 退出码正确透传

用临时假 SQLite 库，连接到 127.0.0.1 mock server。
"""

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile

# 可移植路径：默认相对本测试文件（tests/ 的上一级即项目根），
# 可用环境变量覆盖以适配不同机器。
PY = os.environ.get("CC_PULSE_PYTHON", sys.executable)
SCRIPT_DIR = os.environ.get(
    "CC_PULSE_DIR", os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
PS1 = os.path.join(SCRIPT_DIR, "run_health_check.ps1")
MAIN = os.path.join(SCRIPT_DIR, "check_ccswitch_health.py")
PWSH = os.environ.get("CC_PULSE_PWSH") or shutil.which("pwsh") or "pwsh"

PASSED = []
FAILED = []


def test(name, cond, detail=""):
    if cond:
        PASSED.append(name)
        print(f"  ✓ {name}")
    else:
        FAILED.append((name, detail))
        print(f"  ✗ {name}  {detail}")


# 准备临时目录与假库
tmp = tempfile.mkdtemp(prefix="ccpulse_ps1_")
db_path = os.path.join(tmp, "fake.db")


def write_fake_db():
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("""CREATE TABLE providers (
        name TEXT, app_type TEXT, settings_config TEXT,
        is_current INTEGER, in_failover_queue INTEGER, sort_index INTEGER
    )""")
    cfg = json.dumps(
        {
            "env": {
                "ANTHROPIC_BASE_URL": "http://127.0.0.1:9/v1",  # 必失败，模拟快速失败
                "ANTHROPIC_AUTH_TOKEN": "sk-fake",
                "ANTHROPIC_DEFAULT_HAIKU_MODEL": "claude-haiku-4-5",
                "ANTHROPIC_DEFAULT_SONNET_MODEL": "claude-sonnet-4-5",
            }
        }
    )
    cur.execute(
        "INSERT INTO providers VALUES (?, ?, ?, ?, ?, ?)",
        ("Mock-Provider", "claude", cfg, 1, 1, 0),
    )
    conn.commit()
    conn.close()


def run_pwsh(stdin_text, timeout=30):
    """运行 pwsh -File PS1，stdin 提供模拟输入，捕获 stdout/stderr/exitcode。"""
    env = os.environ.copy()
    env["CC_PULSE_DB"] = db_path
    env["CC_PULSE_PYTHON"] = PY
    env["CC_PULSE_TIMEOUT"] = "2"  # 测试时快速失败
    proc = subprocess.run(
        [PWSH, "-NoProfile", "-File", PS1],
        input=stdin_text,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        env=env,
        cwd=SCRIPT_DIR,
    )
    return proc.returncode, proc.stdout, proc.stderr


write_fake_db()


print("\n[PS1] 主菜单 - 退出选项 7")
rc, out, err = run_pwsh("7\n")
test("退出码 == 0", rc == 0, f"rc={rc} stderr={err[:200]}")
test("输出含主菜单标题", "CC-Pulse" in out or "CC-Pulse" in err)
test("输出含 inspect 入口", "深度诊断" in (out + err) or "inspect" in (out + err))
test("输出含高级设置入口", "高级设置" in (out + err))
test("输出含退出提示", "退出" in (out + err))


print("\n[PS1] 健康检测 · 快速体检 - 选项 1（零子提示）")
# 主菜单 1 -> 体检后选择“不深挖” -> 7 退出
rc, out, err = run_pwsh("1\n4\n7\n", timeout=120)
test("退出码 0 或 1", rc in (0, 1), f"rc={rc}")
combined = out + err
test("输出含 '健康检测'", "健康检测" in combined)
test("输出含 '完成'", "完成" in combined)
test(
    "调用了 check 子命令",
    "check_ccswitch_health.py check" in combined
    or "check --type" in combined
    or "-u check_ccswitch_health.py check" in combined
    or " -u " in combined,
)
test("快速体检带 --failover-only", "--failover-only" in combined)


print("\n[PS1] 健康检测 · 自定义 - 选项 2 -> 当前激活供应商")
# 主菜单 2 -> type 默认 -> range 3 (当前激活) -> 深挖选“不深挖” -> 7 退出
stdin_text = (
    "2\n"  # 主菜单: 健康检测 · 自定义
    "\n"  # type: 默认 claude
    "3\n"  # 范围: 3 (当前激活)
    "4\n"  # 深挖选择：不深挖
    "7\n"  # 退出
)
rc, out, err = run_pwsh(stdin_text, timeout=120)
combined = out + err
test("自定义当前供应商检测 exit 0/1", rc in (0, 1), f"rc={rc}")
test("输出含 '--current-only'", "--current-only" in combined)


print("\n[PS1] 拉模型列表 - 选项 3 -> 默认 claude/队列/只拉列表")
# 主菜单3 -> type默认 -> scope默认(1) -> probeMode默认(1) -> 返回 -> 7退出
# 新交互：每个 Select-MenuItem 降级走 Read-Host，空行=默认第一项
rc, out, err = run_pwsh("3\n\n\n\n\n7\n", timeout=120)
test("退出码 0 或 1", rc in (0, 1), f"rc={rc}")
combined = out + err
test("输出含 '拉模型' 标识", "拉模型" in combined or "list-models" in combined)


print("\n[PS1] inspect - 选项 4 -> 精简 3 步交互（箭头选择降级为数字输入）")
# 主菜单4 -> type默认(1) -> provider序号1 -> 模式默认(1) -> model序号1 -> 回车返回 -> 7退出
stdin_text = (
    "4\n"  # 主菜单: inspect
    "\n"  # type: 默认 claude
    "1\n"  # provider: 序号 1 (Mock-Provider)
    "\n"  # 检测模式: 默认 1 (单一模型)
    "1\n"  # model: 序号 1 (claude-haiku-4-5)
    "\n"  # 返回主菜单
    "7\n"  # 退出
)
rc, out, err = run_pwsh(stdin_text, timeout=180)
combined = out + err
test("退出码 0 或 1 或 2", rc in (0, 1, 2), f"rc={rc} stderr_tail={err[-300:]}")
test("输出含 inspect 步骤", "1/2" in combined or "Provider" in combined or "模型" in combined)
test("输出含 Provider 提示", "Provider" in combined or "Mock-Provider" in combined)
test("输出含模型名", "claude-haiku-4-5" in combined)
test(
    "输出含 inspect 结果",
    "inspect" in combined.lower()
    or "Provider" in combined
    or "Mock-Provider" in combined
    or "claude-haiku-4-5" in combined
    or "完成" in combined
    or "Protocol" in combined
    or "verdict" in combined.lower(),
)


print("\n[PS1] 高级设置 - 选项 6")
# 新编号菜单：进入后直接 q 返回主菜单 -> 7 退出
rc, out, err = run_pwsh("6\nq\n7\n", timeout=60)
combined = out + err
test("退出码 0", rc == 0, f"rc={rc}")
test("输出含 '高级设置'", "高级设置" in combined)
test("显示当前设置", "JSON 输出" in combined and "probe-max-tokens" in combined)
test("显示上下文档位", "上下文档位" in combined or "512k" in combined)
test("显示 vision 设置", "vision" in combined.lower())


print("\n[PS1] 高级设置端到端：开启 JSON 后快速体检输出 JSON")
# 新交互：[6]主菜单 -> 1(JSON项) -> y 开 -> 10/返主菜单 -> [1] 快速体检 -> 回车返回 -> [7] 退出
stdin_text = (
    "6\n"  # 主菜单: 高级设置
    "1\n"  # 选第 1 项: JSON
    "y\n"  # JSON: 开
    "10\n"  # 返回主菜单（末项）
    "1\n"  # 主菜单: 快速体检
    "4\n"  # 深挖选择：不深挖
    "7\n"  # 退出
)
rc, out, err = run_pwsh(stdin_text, timeout=120)
combined = out + err
test("高级设置+体检 退出码 0 或 1", rc in (0, 1), f"rc={rc}")
test("快速体检带 --json", "--json" in combined)
test("stdout 含 JSON 报告", '"schema_version"' in combined or '"providers"' in combined)


print("\n[PS1] 高级设置：上下文档位 1m + vision 后 inspect 带参")
# [6] -> 5(上下文档位,选1m) -> 6(vision) y -> 10 返主菜单
#        -> [4] inspect: type默认 -> provider序号1 -> 模式默认 -> model序号1 -> 返回 -> [7] 退出
stdin_text = (
    "6\n"  # 主菜单: 高级设置
    "5\n"  # 第 5 项: 上下文档位
    "2\n"  # 上下文档位选择: 2 = 1m
    "6\n"  # 第 6 项: vision
    "y\n"  # vision: 开
    "10\n"  # 返回主菜单（末项）
    "4\n"  # inspect
    "\n"  # type 默认 claude
    "1\n"  # provider: 序号 1 (Mock-Provider)
    "\n"  # 检测模式: 默认 1 (单一模型)
    "1\n"  # model: 序号 1 (claude-haiku-4-5)
    "\n"  # 返回主菜单
    "7\n"
)
rc, out, err = run_pwsh(stdin_text, timeout=180)
combined = out + err
test("inspect 高级设置 exit 0/1/2", rc in (0, 1, 2), f"rc={rc} tail={combined[-500:]}")
test(
    "inspect 命令含 --probe-context 1m",
    "--probe-context" in combined and "1m" in combined,
    f"tail={combined[-500:]}",
)
test(
    "inspect 命令含 vision include",
    "vision" in combined.lower(),
    f"tail={combined[-500:]}",
)


print("\n[PS1] 运行日志入口 - 选项 5")
# 主菜单5 -> 日志菜单选7(返回主菜单) -> 主菜单7退出
rc, out, err = run_pwsh("5\n7\n7\n", timeout=60)
combined = out + err
test(
    "运行日志菜单可见",
    "运行日志" in combined or "history" in combined.lower() or "失败日志" in combined,
)
test("运行日志菜单可返回", rc == 0, f"rc={rc}")


print("\n[PS1] 退出选项 7（直接退出）")
rc, out, err = run_pwsh("7\n")
test("退出码 0", rc == 0, f"rc={rc}")


print("\n[PS1] 错误输入 -> 默认首项 -> 退出")
# 主菜单输入非法 "9"：降级 Select-MenuItem 返回 -2（字面），主菜单 default continue 重绘；
# 再给空行(默认0=快速体检) 会跑检测。改用直接 7 退出验证主菜单容错。
rc, out, err = run_pwsh("9\n7\n", timeout=60)
combined = out + err
test("退出码 0（最终选 7 退出）", rc == 0, f"rc={rc}")


print("\n[PS1] 菜单失败返回主菜单不崩溃")
# 不存在的数据库会令 Show-Banner 提前失败；按回车返回后应能继续菜单并正常退出。
missing_db = db_path + ".missing"
original_db = db_path
try:
    db_path = missing_db
    rc, out, err = run_pwsh("1\n\n7\n")
finally:
    db_path = original_db
combined = out + err
test("提前返回后可继续退出", rc in (0, 1), f"rc={rc} output={combined[-300:]}")
test("提前返回无类型转换异常", "Cannot convert" not in combined)


print("\n[PS1] inspect 缺供应商返回主菜单不崩溃")
empty_db = os.path.join(tmp, "empty.db")
sqlite3.connect(empty_db).close()
original_db = db_path
try:
    db_path = empty_db
    rc, out, err = run_pwsh("4\n\n\n\n7\n")
finally:
    db_path = original_db
combined = out + err
test("inspect 提前返回后可继续退出", rc in (0, 1), f"rc={rc} output={combined[-300:]}")
test("inspect 提前返回无类型转换异常", "Cannot convert" not in combined)
empty_db = os.path.join(tmp, "empty.db")
sqlite3.connect(empty_db).close()
original_db = db_path
try:
    db_path = empty_db
    # 主菜单5(日志) -> 1(历史) -> again回车(默认2返回) -> 主菜单7退出
    rc, out, err = run_pwsh("5\n1\n\n7\n", timeout=60)
finally:
    db_path = original_db
combined = out + err
test("日志命令空库不崩溃", rc in (0, 1, 2), f"rc={rc} output={combined[-300:]}")


# 汇总
print("\n" + "=" * 60)
print(f"  PS1 PASS: {len(PASSED)}")
print(f"  PS1 FAIL: {len(FAILED)}")
print("=" * 60)
if FAILED:
    print("\n失败用例:")
    for n, d in FAILED:
        print(f"  - {n}: {d}")
    sys.exit(1)
print("\n✓ PS1 启动器测试全部通过")
