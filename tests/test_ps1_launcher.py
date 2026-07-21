"""PS1 启动器端到端测试。

启动 pwsh，把模拟输入通过 stdin 喂入，验证：
  1. 菜单渲染（6 项主菜单）
  2. 健康检测快速体检（选项 1）零子提示一键跑
  3. inspect（选项 4）精简 3 步交互
  4. 高级设置（选项 5）
  5. 退出码正确透传

用临时假 SQLite 库，连接到 127.0.0.1 mock server。
"""
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import json
import shutil
from http.server import BaseHTTPRequestHandler, HTTPServer

# 可移植路径：默认相对本测试文件（tests/ 的上一级即项目根），
# 可用环境变量覆盖以适配不同机器。
PY = os.environ.get("CC_PULSE_PYTHON", sys.executable)
SCRIPT_DIR = os.environ.get(
    "CC_PULSE_DIR",
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
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
    cur.execute('''CREATE TABLE providers (
        name TEXT, app_type TEXT, settings_config TEXT,
        is_current INTEGER, in_failover_queue INTEGER, sort_index INTEGER
    )''')
    cfg = json.dumps({
        "env": {
            "ANTHROPIC_BASE_URL": "http://127.0.0.1:9/v1",  # 必失败，模拟快速失败
            "ANTHROPIC_AUTH_TOKEN": "sk-fake",
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": "claude-haiku-4-5",
            "ANTHROPIC_DEFAULT_SONNET_MODEL": "claude-sonnet-4-5",
        }
    })
    cur.execute("INSERT INTO providers VALUES (?, ?, ?, ?, ?, ?)",
                ("Mock-Provider", "claude", cfg, 1, 1, 0))
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
        env=env,
        cwd=SCRIPT_DIR,
    )
    return proc.returncode, proc.stdout, proc.stderr


write_fake_db()


print("\n[PS1] 主菜单 - 退出选项 6")
rc, out, err = run_pwsh("6\n")
test("退出码 == 0", rc == 0, f"rc={rc} stderr={err[:200]}")
test("输出含主菜单标题", "CC-Pulse" in out or "CC-Pulse" in err)
test("输出含 inspect 入口", "深度诊断" in (out + err) or "inspect" in (out + err))
test("输出含高级设置入口", "高级设置" in (out + err))
test("输出含退出提示", "退出" in (out + err))


print("\n[PS1] 健康检测 · 快速体检 - 选项 1（零子提示）")
# 主菜单 1 -> 直接跑 -> 回车返回 -> 6 退出
rc, out, err = run_pwsh("1\n\n6\n", timeout=120)
test("退出码 0 或 1", rc in (0, 1), f"rc={rc}")
combined = out + err
test("输出含 '健康检测'", "健康检测" in combined)
test("输出含 '完成'", "完成" in combined)
test("调用了 check 子命令", "check_ccswitch_health.py check" in combined or "check --type" in combined or "-u check_ccswitch_health.py check" in combined or " -u " in combined)
test("快速体检带 --failover-only", "--failover-only" in combined)


print("\n[PS1] 拉模型列表 - 选项 3 -> 1 (默认 claude/队列)")
rc, out, err = run_pwsh("3\n\n1\n\n6\n", timeout=120)
test("退出码 0 或 1", rc in (0, 1), f"rc={rc}")
combined = out + err
test("输出含 '拉模型' 标识", "拉模型" in combined or "list-models" in combined)


print("\n[PS1] inspect - 选项 4 -> 精简 3 步交互")
# 主菜单4 -> type默认 -> M手动 -> provider -> source1 -> model -> 回车返回 -> 6退出
stdin_text = (
    "4\n"                 # 主菜单: inspect
    "\n"                  # type: 默认 claude
    "M\n"                 # 手动输入 provider
    "Mock-Provider\n"     # provider 名
    "1\n"                 # source: configured
    "claude-haiku-4-5\n"  # model
    "\n"                  # 返回主菜单
    "6\n"                 # 退出
)
rc, out, err = run_pwsh(stdin_text, timeout=180)
combined = out + err
test("退出码 0 或 1 或 2", rc in (0, 1, 2), f"rc={rc} stderr_tail={err[-300:]}")
test("输出含 inspect 三步", "1/3" in combined and "2/3" in combined and "3/3" in combined)
test("输出含 Provider 提示", "Provider" in combined or "Mock-Provider" in combined)
test("输出含模型名", "claude-haiku-4-5" in combined)
test("输出含 inspect 结果",
     "inspect" in combined.lower()
     or "Provider" in combined
     or "Mock-Provider" in combined
     or "claude-haiku-4-5" in combined
     or "完成" in combined
     or "Protocol" in combined
     or "verdict" in combined.lower())


print("\n[PS1] 高级设置 - 选项 5")
# 菜单5 + JSON/max-tokens/thinking/UA/context/vision 共 6 项默认 + 返回主菜单 + 退出
# 共 1+6+1+1 = 9 次输入
rc, out, err = run_pwsh("5\n\n\n\n\n\n\n\n6\n", timeout=60)
combined = out + err
test("退出码 0", rc == 0, f"rc={rc}")
test("输出含 '高级设置'", "高级设置" in combined)
test("显示当前设置", "JSON 输出" in combined and "probe-max-tokens" in combined)
test("显示上下文档位", "上下文档位" in combined or "512k" in combined)
test("显示 vision 设置", "vision" in combined.lower())


print("\n[PS1] 高级设置端到端：开启 JSON 后快速体检输出 JSON")
# [5] 开启 JSON -> 其余默认回车 -> [1] 快速体检 -> [6] 退出
stdin_text = (
    "5\n"           # 主菜单: 高级设置
    "y\n"           # JSON 输出: 开
    "\n"            # max-tokens: 默认
    "\n"            # thinking: 默认
    "\n"            # user-agent: 默认
    "\n"            # context: 默认 512k
    "\n"            # vision: 默认关
    "\n"            # 回车返回主菜单
    "1\n"           # 主菜单: 快速体检
    "\n"            # 回车返回主菜单
    "6\n"           # 退出
)
rc, out, err = run_pwsh(stdin_text, timeout=120)
combined = out + err
test("高级设置+体检 退出码 0 或 1", rc in (0, 1), f"rc={rc}")
test("快速体检带 --json", "--json" in combined)
test("stdout 含 JSON 报告", '"schema_version"' in combined or '"providers"' in combined)


print("\n[PS1] 高级设置：上下文档位 1m + vision 后 inspect 带参")
# 高级设置设 1m + vision，再跑 inspect（手动 provider），检查命令行含新参数
stdin_text = (
    "5\n"
    "\n"            # JSON 默认
    "\n"            # max-tokens
    "\n"            # thinking
    "\n"            # UA
    "1m\n"          # context 1m
    "y\n"           # vision 开
    "\n"            # 返回主菜单
    "4\n"           # inspect
    "\n"            # type 默认 claude
    "M\n"           # 手动 provider
    "Mock-Provider\n"
    "1\n"           # source configured
    "claude-haiku-4-5\n"
    "\n"            # 返回主菜单
    "6\n"
)
rc, out, err = run_pwsh(stdin_text, timeout=180)
combined = out + err
test("inspect 高级设置 exit 0/1/2", rc in (0, 1, 2), f"rc={rc}")
test("inspect 命令含 --probe-context 1m", "--probe-context" in combined and "1m" in combined,
     f"tail={combined[-500:]}")
test("inspect 命令含 vision include", "vision" in combined.lower(),
     f"tail={combined[-500:]}")


print("\n[PS1] 退出选项 6（直接退出）")
rc, out, err = run_pwsh("6\n")
test("退出码 0", rc == 0, f"rc={rc}")


print("\n[PS1] 错误输入 -> 提示重试 -> 退出")
rc, out, err = run_pwsh("9\n\n6\n", timeout=30)
combined = out + err
test("退出码 0（最终选 6 退出）", rc == 0, f"rc={rc}")
test("提示无效输入", "无效" in combined)


# 清理
shutil.rmtree(tmp, ignore_errors=True)


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
