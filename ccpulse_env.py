"""环境变量覆盖检测（env-check）：探测 env 是否会静默覆盖 cc-switch 所选供应商。"""

import json
import os
from typing import Any

from ccpulse_output import _pad

# env 变量 → (tool, 配置取值类别)
# 取值类别: base_url=供应商 base_url；token=供应商 api_key；tier:xxx=对应档位模型
_ENV_VARS: list[tuple[str, str, str]] = [
    ("ANTHROPIC_BASE_URL", "claude", "base_url"),
    ("ANTHROPIC_AUTH_TOKEN", "claude", "token"),
    ("ANTHROPIC_API_KEY", "claude", "token"),
    ("ANTHROPIC_MODEL", "claude", "tier:default"),
    ("ANTHROPIC_DEFAULT_HAIKU_MODEL", "claude", "tier:haiku"),
    ("ANTHROPIC_DEFAULT_SONNET_MODEL", "claude", "tier:sonnet"),
    ("ANTHROPIC_DEFAULT_OPUS_MODEL", "claude", "tier:opus"),
    ("ANTHROPIC_DEFAULT_FABLE_MODEL", "claude", "tier:fable"),
    ("OPENAI_BASE_URL", "codex", "base_url"),
    ("OPENAI_API_KEY", "codex", "token"),
    ("OPENAI_MODEL", "codex", "tier:default"),
    ("OPENAI_BASE_URL", "openclaw", "base_url"),
    ("OPENAI_API_KEY", "openclaw", "token"),
]

_SECRET_ENV = {"ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"}


def _mask(value: str) -> str:
    """密钥掩码：保留前 6 位 + ***，绝不打印完整明文。"""
    if not value:
        return ""
    return value[:6] + "***" if len(value) > 6 else "***"


def _current_provider(providers: list, tool: str) -> Any:
    for p in providers:
        if getattr(p, "app_type", None) == tool and getattr(p, "is_current", False):
            return p
    return None


def _tier_model(provider: Any, tier: str) -> str:
    for t in getattr(provider, "tiers", []):
        if getattr(t, "tier", None) == tier:
            return getattr(t, "model", None) or getattr(t, "raw_model", None) or ""
    return ""


def _config_value(provider: Any, kind: str) -> str:
    """从供应商对象取 env 变量对应的配置值（原始值，未掩码）。"""
    if kind == "base_url":
        return getattr(provider, "base_url", "") or ""
    if kind == "token":
        return getattr(provider, "api_key", "") or ""
    if kind.startswith("tier:"):
        return _tier_model(provider, kind.split(":", 1)[1])
    return ""


# severity -> 中文标签（用于人类表格第二行的前缀）
_SEVERITY_LABEL = {"conflict": "冲突", "override": "生效中", "info": "一致"}


def _truncate(value: str, width: int = 22) -> str:
    """值超宽时截断到 width-1 字符并加省略号，避免长 URL 撑破列宽。"""
    if len(value) <= width:
        return value
    return value[: width - 1] + "…"


def env_check_findings(providers: list, environ: dict | None = None) -> list[dict]:
    """对比环境变量与 current provider 配置，产出 findings。

    severity:
      conflict  env 已设置且与 current provider 配置不一致 → 会静默覆盖
      override  env 已设置但无 current provider 可核对 → 环境变量生效中
      info      env 已设置且与 current provider 配置一致
    未设置的变量不产生 finding。
    """
    environ = os.environ if environ is None else environ
    findings: list[dict] = []
    for env_var, tool, kind in _ENV_VARS:
        if env_var not in environ:
            continue
        env_value = environ[env_var]
        secret = env_var in _SECRET_ENV
        current = _current_provider(providers, tool)
        if current is None:
            findings.append(
                {
                    "tool": tool,
                    "env_var": env_var,
                    "env_value": _mask(env_value) if secret else env_value,
                    "config_value": "",
                    "impact": f"{tool} 无 current provider，{env_var} 环境变量生效中",
                    "severity": "override",
                }
            )
            continue
        raw_config = _config_value(current, kind)
        masked_env = _mask(env_value) if secret else env_value
        masked_cfg = _mask(raw_config) if secret else raw_config
        if raw_config and env_value == raw_config:
            severity, impact = "info", f"与 current provider {current.name} 配置一致"
        else:
            severity, impact = (
                "conflict",
                f"环境变量将覆盖 current provider {current.name} 的配置",
            )
        findings.append(
            {
                "tool": tool,
                "provider": current.name,
                "env_var": env_var,
                "env_value": masked_env,
                "config_value": masked_cfg,
                "impact": impact,
                "severity": severity,
            }
        )
    return findings


def run_env_check(args: Any, providers: list, say: Any) -> int:
    """env-check 子命令入口：人类表格 / JSON 输出，有 conflict 返回 2。"""
    findings = env_check_findings(providers)
    conflicts = sum(1 for f in findings if f["severity"] == "conflict")
    if getattr(args, "json", False):
        report = {
            "schema_version": 1,
            "command": "env-check",
            "findings": findings,
            "summary": {"conflicts": conflicts, "findings": len(findings)},
        }
        print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    else:
        if findings:
            say(
                f"{_pad('tool', 10)} {_pad('env_var', 30)} "
                f"{_pad('当前值', 22)} {_pad('配置值', 22)}"
            )
            say("-" * 100)
            for f in findings:
                say(
                    f"{_pad(f['tool'], 10)} {_pad(f['env_var'], 30)} "
                    f"{_pad(_truncate(f['env_value']), 22)} "
                    f"{_pad(_truncate(f['config_value']), 22)}"
                )
                say(f"  -> {_SEVERITY_LABEL[f['severity']]}：{f['impact']}")
        if conflicts:
            say(f"检测到 {conflicts} 处冲突：环境变量会覆盖 cc-switch 所选供应商")
        else:
            say("环境变量无冲突：cc-switch 所选供应商不受 env 覆盖")
    return 2 if conflicts else 0
