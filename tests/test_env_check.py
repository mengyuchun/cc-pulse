"""ccpulse_env 环境变量覆盖检测 单元测试（纯标准库，不依赖 pytest）。"""

import importlib.util
import io
import json
import os
import sys
from dataclasses import dataclass, field
from types import SimpleNamespace

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
SCRIPT = os.path.join(_ROOT, "ccpulse_env.py")
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# 通过 importlib 加载项目模块
spec = importlib.util.spec_from_file_location("ccpulse_env", SCRIPT)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


PASSED = []
FAILED = []


def test(name, cond, detail=""):
    if cond:
        PASSED.append(name)
        print(f"  ✓ {name}")
    else:
        FAILED.append((name, detail))
        print(f"  ✗ {name}  {detail}")


# 假 Provider / Tier（duck-typing，不必真调主模块）
@dataclass
class Tier:
    tier: str
    model: str


@dataclass
class FakeProvider:
    name: str
    app_type: str
    base_url: str
    api_key: str = ""
    auth_mode: str = "apikey"
    tiers: list = field(default_factory=list)
    is_current: bool = False


def provider(
    name="p1",
    app_type="claude",
    base_url="https://a.example/v1",
    api_key="sk-abcdef123456",
    is_current=True,
    tiers=None,
):
    return FakeProvider(
        name,
        app_type,
        base_url,
        api_key,
        "apikey",
        tiers or [Tier("default", "claude-sonnet-4-5")],
        is_current,
    )


# ============ 纯函数 env_check_findings ============

print("\n[env_check_findings] base_url 冲突")
provs = [provider(base_url="https://a.example/v1")]
fs = mod.env_check_findings(provs, {"ANTHROPIC_BASE_URL": "https://b.example/v1"})
test(
    "base_url 冲突 → conflict",
    len(fs) == 1
    and fs[0]["severity"] == "conflict"
    and fs[0]["env_var"] == "ANTHROPIC_BASE_URL"
    and fs[0]["provider"] == "p1"
    and fs[0]["config_value"] == "https://a.example/v1",
    f"fs={fs}",
)

print("\n[env_check_findings] token 掩码")
key = "sk-abcdef1234567890"
provs = [provider(api_key=key)]
fs = mod.env_check_findings(provs, {"ANTHROPIC_AUTH_TOKEN": key})
text = json.dumps(fs, ensure_ascii=False)
test(
    "token 掩码不含明文且保留前 6 位",
    key not in text
    and fs[0]["env_value"] == "sk-abc***"
    and fs[0]["config_value"] == "sk-abc***",
    f"text={text}",
)

print("\n[env_check_findings] 与 current 一致 → info")
provs = [provider(base_url="https://a.example/v1")]
fs = mod.env_check_findings(provs, {"ANTHROPIC_BASE_URL": "https://a.example/v1"})
test(
    "一致 → info",
    len(fs) == 1 and fs[0]["severity"] == "info",
    f"fs={fs}",
)

print("\n[env_check_findings] 无 current provider → override")
provs = [provider(is_current=False)]
fs = mod.env_check_findings(provs, {"ANTHROPIC_BASE_URL": "https://x.example/v1"})
test(
    "无 current → override 且无 provider 键",
    len(fs) == 1 and fs[0]["severity"] == "override" and "provider" not in fs[0],
    f"fs={fs}",
)

print("\n[env_check_findings] 未设置变量 → 无 finding")
fs = mod.env_check_findings([], {"PATH": "/usr/bin"})
fs2 = mod.env_check_findings([provider()], {"PATH": "/usr/bin"})
test("空 providers + 无关 env → 无 finding", fs == [])
test("有 providers + 无关 env → 无 finding", fs2 == [])

print("\n[env_check_findings] 模型档位")
tiers = [Tier("haiku", "claude-haiku-4-5"), Tier("default", "claude-sonnet-4-5")]
provs = [provider(tiers=tiers)]
fs = mod.env_check_findings(
    provs, {"ANTHROPIC_DEFAULT_HAIKU_MODEL": "claude-haiku-4-5"}
)
test("模型档位一致 → info", len(fs) == 1 and fs[0]["severity"] == "info", f"fs={fs}")
fs = mod.env_check_findings(
    provs, {"ANTHROPIC_DEFAULT_HAIKU_MODEL": "claude-haiku-4-6"}
)
test(
    "模型档位冲突 → conflict",
    len(fs) == 1 and fs[0]["severity"] == "conflict",
    f"fs={fs}",
)

print("\n[env_check_findings] codex/openclaw 共用 OPENAI_BASE_URL")
provs = [provider(name="oc", app_type="openclaw", base_url="https://o.example/v1")]
fs = mod.env_check_findings(provs, {"OPENAI_BASE_URL": "https://o.example/v1"})
tools = {f["tool"]: f["severity"] for f in fs}
test(
    "codex 无 current → override；openclaw 一致 → info",
    tools.get("codex") == "override" and tools.get("openclaw") == "info",
    f"tools={tools}",
)


# ============ run_env_check 退出码 ============

print("\n[run_env_check] 退出码与输出")
lines = []


def collect(*a, **k):
    lines.append(" ".join(str(x) for x in a))


_saved = dict(os.environ)


def with_environ(env):
    os.environ.clear()
    os.environ.update(env)


def restore_environ():
    os.environ.clear()
    os.environ.update(_saved)


args = SimpleNamespace(json=False)

with_environ({"ANTHROPIC_BASE_URL": "https://b.example/v1"})
provs = [provider(base_url="https://a.example/v1")]
try:
    rc = mod.run_env_check(args, provs, collect)
finally:
    restore_environ()
test("有冲突 → 退出码 2", rc == 2, f"rc={rc}")
test("有冲突 → 汇总行", any("检测到 1 处冲突" in x for x in lines), f"lines={lines}")

lines.clear()
with_environ({})
provs = [provider(base_url="https://a.example/v1")]
try:
    rc = mod.run_env_check(args, provs, collect)
finally:
    restore_environ()
test("无冲突 → 退出码 0", rc == 0, f"rc={rc}")
test("无冲突 → 提示行", any("环境变量无冲突" in x for x in lines), f"lines={lines}")

print("\n[run_env_check] JSON 输出")
args = SimpleNamespace(json=True)
with_environ({"ANTHROPIC_BASE_URL": "https://b.example/v1"})
provs = [provider(base_url="https://a.example/v1")]
buf = io.StringIO()
old_stdout = sys.stdout
sys.stdout = buf
try:
    rc = mod.run_env_check(args, provs, collect)
finally:
    sys.stdout = old_stdout
    restore_environ()
try:
    rep = json.loads(buf.getvalue())
except (json.JSONDecodeError, ValueError):
    rep = {}
test(
    "JSON report 结构 + 退出码 2",
    rc == 2
    and rep.get("command") == "env-check"
    and rep.get("schema_version") == 1
    and rep["summary"]["conflicts"] == 1
    and len(rep["findings"]) == 1,
    f"rc={rc} rep={rep}",
)


# ============ 汇总 ============

print("\n" + "=" * 60)
print(f"  PASS: {len(PASSED)}")
print(f"  FAIL: {len(FAILED)}")
print("=" * 60)
if FAILED:
    print("\n失败用例:")
    for n, d in FAILED:
        print(f"  - {n}: {d}")
    sys.exit(1)
