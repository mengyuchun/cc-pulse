"""环境变量覆盖检测（env-check）：探测 env 是否会静默覆盖 cc-switch 所选供应商。"""

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
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
    """密钥掩码：绝不打印完整明文；短密钥（≤10 位）最多保留前 2 位。"""
    if not value:
        return ""
    if len(value) <= 6:
        return "***"
    if len(value) <= 10:
        return value[:2] + "***"
    return value[:6] + "***"


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


@dataclass(frozen=True)
class _StandaloneTier:
    tier: str
    model: str
    raw_model: str = ""


@dataclass(frozen=True)
class _StandaloneProvider:
    name: str
    app_type: str
    base_url: str
    api_key: str = ""
    auth_mode: str = "apikey"
    tiers: list = field(default_factory=list)
    is_current: bool = True


def _load_toml_minimal(path: Path) -> dict[str, Any]:
    """加载 TOML（3.11+ 用 tomllib，3.10 用纯标准库安全子集解析器）。"""
    raw = path.read_text(encoding="utf-8", errors="replace")
    try:
        import tomllib

        return tomllib.loads(raw)
    except ImportError:
        pass
    root: dict[str, Any] = {}
    cur = root
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        m_sec = re.match(r"^\[([^\]]+)\]\s*(?:#.*)?$", stripped)
        if m_sec:
            cur = root
            for part in m_sec.group(1).split("."):
                part = part.strip().strip('"').strip("'")
                cur = cur.setdefault(part, {})
            continue
        m_kv = re.match(r"^([A-Za-z0-9_.-]+)\s*=\s*(.+)$", stripped)
        if m_kv:
            k, v = m_kv.group(1), m_kv.group(2).strip()
            # 去掉未引号内联注释并解引号
            if (v.startswith('"') and '"' in v[1:]) or (
                v.startswith("'") and "'" in v[1:]
            ):
                q = v[0]
                end = v.find(q, 1)
                cur[k] = v[1:end]
            else:
                v = re.sub(r"\s+#.*$", "", v).strip()
                if v.lower() == "true":
                    cur[k] = True
                elif v.lower() == "false":
                    cur[k] = False
                else:
                    cur[k] = v
    return root


def load_standalone_env_providers(home_dir: Path | None = None) -> list:
    """无 cc-switch.db 时读取本地原生配置文件用于 env-check：
    - ~/.claude/settings.json（env 块）
    - ~/.codex/config.toml + ~/.codex/auth.json
    - ~/.cc-pulse/providers.toml（可选）
    """
    home = Path.home() if home_dir is None else Path(home_dir)
    out: list = []

    pulse_toml = home / ".cc-pulse" / "providers.toml"
    if pulse_toml.exists():
        try:
            data = _load_toml_minimal(pulse_toml)
            provs = data.get("providers")
            items = provs.items() if isinstance(provs, dict) else []
            for pname, pcfg in items:
                if not isinstance(pcfg, dict):
                    continue
                base = str(pcfg.get("base_url") or "").strip()
                key = str(pcfg.get("api_key") or "").strip()
                if not key and pcfg.get("api_key_env"):
                    key = os.environ.get(str(pcfg["api_key_env"]), "").strip()
                app_type = str(
                    pcfg.get("app_type") or pcfg.get("type") or "claude"
                ).strip()
                model = str(pcfg.get("model") or "").strip()
                tiers = []
                for tname in ("haiku", "sonnet", "opus", "fable", "default"):
                    tm = str(pcfg.get(tname) or "").strip()
                    if tm:
                        tiers.append(_StandaloneTier(tname, tm, tm))
                if not tiers and model:
                    tiers.append(_StandaloneTier("default", model, model))
                if base or key or tiers:
                    out.append(
                        _StandaloneProvider(
                            name=str(pname),
                            app_type=app_type,
                            base_url=base,
                            api_key=key,
                            auth_mode=str(pcfg.get("auth_mode") or "authtoken"),
                            tiers=tiers,
                            is_current=bool(pcfg.get("is_current", True)),
                        )
                    )
        except (OSError, ValueError, TypeError):
            pass

    claude_settings = home / ".claude" / "settings.json"
    if claude_settings.exists():
        try:
            cfg = json.loads(
                claude_settings.read_text(encoding="utf-8", errors="replace")
            )
            env = cfg.get("env") if isinstance(cfg, dict) else None
            if isinstance(env, dict):
                base = str(env.get("ANTHROPIC_BASE_URL") or "").strip()
                token = str(
                    env.get("ANTHROPIC_AUTH_TOKEN")
                    or env.get("ANTHROPIC_API_KEY")
                    or ""
                ).strip()
                tiers = []
                for tier_name, key in (
                    ("haiku", "ANTHROPIC_DEFAULT_HAIKU_MODEL"),
                    ("sonnet", "ANTHROPIC_DEFAULT_SONNET_MODEL"),
                    ("opus", "ANTHROPIC_DEFAULT_OPUS_MODEL"),
                    ("fable", "ANTHROPIC_DEFAULT_FABLE_MODEL"),
                    ("default", "ANTHROPIC_MODEL"),
                ):
                    m = str(env.get(key) or "").strip()
                    if m:
                        tiers.append(_StandaloneTier(tier_name, m, m))
                if base or token or tiers:
                    out.append(
                        _StandaloneProvider(
                            name="claude-settings",
                            app_type="claude",
                            base_url=base,
                            api_key=token,
                            auth_mode="authtoken"
                            if env.get("ANTHROPIC_AUTH_TOKEN")
                            else "apikey",
                            tiers=tiers,
                            is_current=True,
                        )
                    )
        except (OSError, ValueError, TypeError):
            pass

    codex_toml = home / ".codex" / "config.toml"
    if codex_toml.exists():
        try:
            tdata = _load_toml_minimal(codex_toml)
            mp_name = tdata.get("model_provider")
            mp_tbl = (
                (tdata.get("model_providers") or {}).get(mp_name, {}) if mp_name else {}
            )
            base = str(
                (mp_tbl.get("base_url") if isinstance(mp_tbl, dict) else "")
                or tdata.get("base_url")
                or ""
            ).strip()
            model = str(tdata.get("model") or "").strip()
            key = ""
            codex_auth = home / ".codex" / "auth.json"
            if codex_auth.exists():
                try:
                    adata = json.loads(
                        codex_auth.read_text(encoding="utf-8", errors="replace")
                    )
                    if isinstance(adata, dict):
                        key = str(adata.get("OPENAI_API_KEY") or "").strip()
                except (OSError, ValueError, TypeError):
                    pass
            tiers = [_StandaloneTier("default", model, model)] if model else []
            if base or key or tiers:
                out.append(
                    _StandaloneProvider(
                        name=str(mp_name or "codex-config"),
                        app_type="codex",
                        base_url=base,
                        api_key=key,
                        auth_mode="authtoken",
                        tiers=tiers,
                        is_current=True,
                    )
                )
        except (OSError, ValueError, TypeError):
            pass

    return out


def run_env_check(args: Any, providers: list, say: Any) -> int:
    """env-check 子命令入口：人类表格 / JSON 输出，有 conflict 返回 2。"""
    source = "cc-switch"
    if not providers and not getattr(args, "no_standalone", False):
        standalone = load_standalone_env_providers()
        if standalone:
            providers = standalone
            source = "standalone"
    findings = env_check_findings(providers)
    conflicts = sum(1 for f in findings if f["severity"] == "conflict")
    if getattr(args, "json", False):
        report = {
            "schema_version": 1,
            "command": "env-check",
            "config_source": source if providers else "env-only",
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
            say(f"检测到 {conflicts} 处冲突：环境变量会覆盖当前配置")
        elif findings:
            say("环境变量无冲突：当前环境变量与配置一致")
        else:
            say("环境变量无冲突：未检测到会静默覆盖配置的环境变量")
    return 2 if conflicts else 0
