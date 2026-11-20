"""无参数交互终端（如双击 .exe）时的纯标准库跨平台向导菜单。"""

from __future__ import annotations

import getpass
import os
from typing import Callable

from cc_pulse import __version__

_TEMP_KEY_ENV = "_CC_PULSE_INTERACTIVE_KEY"


def _prompt_line(prompt: str, input_fn: Callable[[str], str]) -> str:
    return input_fn(prompt).strip()


def _prompt_protocol(
    input_fn: Callable[[str], str], print_fn: Callable[[str], None]
) -> str | None:
    print_fn(
        "  协议类型：1=自动推断(默认)  2=anthropic  3=openai_chat  4=openai_responses"
    )
    raw = _prompt_line("  请选择 [1-4，回车默认 1]: ", input_fn)
    return {
        "2": "anthropic",
        "3": "openai_chat",
        "4": "openai_responses",
    }.get(raw)


def _run_with_temp_key(
    api_key: str,
    argv: list[str],
    run_cli: Callable[[list[str]], int],
) -> int:
    had_prev = _TEMP_KEY_ENV in os.environ
    prev_val = os.environ.get(_TEMP_KEY_ENV)
    if api_key:
        os.environ[_TEMP_KEY_ENV] = api_key
    try:
        return run_cli(argv)
    finally:
        if had_prev and prev_val is not None:
            os.environ[_TEMP_KEY_ENV] = prev_val
        else:
            os.environ.pop(_TEMP_KEY_ENV, None)


def _wizard_single_check(
    run_cli: Callable[[list[str]], int],
    input_fn: Callable[[str], str],
    secret_fn: Callable[[str], str],
    print_fn: Callable[[str], None],
) -> int:
    print_fn("\n--- 单端点快速测活 ---")
    base_url = _prompt_line(
        "  中转站 Base URL（如 https://relay.example.com）: ", input_fn
    )
    if not base_url:
        print_fn("  未输入 URL，已取消。")
        return 0
    api_key = secret_fn("  API Key（输入时不回显，留空则尝试读取环境变量）: ").strip()
    proto = _prompt_protocol(input_fn, print_fn)
    model = _prompt_line("  指定模型 ID（可选，回车自动选默认模型）: ", input_fn)
    argv = ["check", "--no-db", "--base-url", base_url]
    if api_key:
        argv.extend(["--api-key-env", _TEMP_KEY_ENV])
    if proto:
        argv.extend(["--protocol", proto])
    if model:
        argv.extend(["--model", model])
    print_fn("")
    return _run_with_temp_key(api_key, argv, run_cli)


def _wizard_single_inspect(
    run_cli: Callable[[list[str]], int],
    input_fn: Callable[[str], str],
    secret_fn: Callable[[str], str],
    print_fn: Callable[[str], None],
) -> int:
    print_fn("\n--- 单模型深度诊断（防掺水 / 云后端归因 / 能力检测）---")
    base_url = _prompt_line(
        "  中转站 Base URL（如 https://relay.example.com）: ", input_fn
    )
    if not base_url:
        print_fn("  未输入 URL，已取消。")
        return 0
    api_key = secret_fn("  API Key（输入时不回显，留空则尝试读取环境变量）: ").strip()
    proto = _prompt_protocol(input_fn, print_fn)
    default_model = (
        "gpt-4o"
        if proto in ("openai_chat", "openai_responses")
        else "claude-sonnet-4-5"
    )
    model = (
        _prompt_line(f"  模型 ID（回车默认 {default_model}）: ", input_fn)
        or default_model
    )
    fmt_raw = _prompt_line(
        "  输出格式：1=脱敏战报卡(默认)  2=完整可读报告 [1-2]: ", input_fn
    )
    fmt = "human" if fmt_raw == "2" else "card"
    auth_full = _prompt_line(
        "  是否追加缓存回放深度探针（多 2 次请求）？[y/N]: ", input_fn
    ).lower() in ("y", "yes")
    argv = [
        "inspect",
        "--no-db",
        "--base-url",
        base_url,
        "--model",
        model,
        "--format",
        fmt,
    ]
    if api_key:
        argv.extend(["--api-key-env", _TEMP_KEY_ENV])
    if proto:
        argv.extend(["--protocol", proto])
    if auth_full:
        argv.append("--auth-full")
    print_fn("")
    return _run_with_temp_key(api_key, argv, run_cli)


def run_interactive_menu(
    run_cli: Callable[[list[str]], int],
    *,
    input_fn: Callable[[str], str] = input,
    secret_fn: Callable[[str], str] = getpass.getpass,
    print_fn: Callable[[str], None] = print,
    pause: bool = True,
) -> int:
    """交互式主菜单：面向双击 .exe 或无参数运行的用户，避免直接开测后闪退。"""
    while True:
        print_fn("")
        print_fn("=" * 60)
        print_fn(f"  CC-Pulse v{__version__} · 中转站可用性与真伪诊断")
        print_fn("=" * 60)
        print_fn("  [1] 环境变量冲突排查（零 Key，推荐第一步）")
        print_fn("  [2] 单端点快速测活（输入中转站 URL + API Key）")
        print_fn("  [3] 单模型深度诊断 + 生成脱敏战报卡（inspect）")
        print_fn("  [4] 检测本机已有配置（Claude Code / Codex / cc-switch）")
        print_fn("  [5] 查看本地探测历史趋势（trend）")
        print_fn("  [6] 查看命令行参数帮助（--help）")
        print_fn("  [0] 退出")
        print_fn("-" * 60)
        try:
            choice = _prompt_line("请输入选项编号 [0-6]: ", input_fn)
        except (EOFError, KeyboardInterrupt):
            print_fn("\n已退出。")
            return 0

        if choice in ("0", "q", "quit", "exit"):
            print_fn("已退出。")
            return 0

        rc = 0
        try:
            if choice == "1":
                print_fn("")
                rc = run_cli(["env-check"])
            elif choice == "2":
                rc = _wizard_single_check(run_cli, input_fn, secret_fn, print_fn)
            elif choice == "3":
                rc = _wizard_single_inspect(run_cli, input_fn, secret_fn, print_fn)
            elif choice == "4":
                print_fn("")
                rc = run_cli(["check"])
            elif choice == "5":
                print_fn("")
                rc = run_cli(["trend"])
            elif choice == "6":
                print_fn("")
                rc = run_cli(["--help"])
            else:
                print_fn(f"无效选项: {choice!r}，请输入 0-6。")
                continue
        except SystemExit as exc:
            rc = exc.code if isinstance(exc.code, int) else 0
        except KeyboardInterrupt:
            print_fn("\n已取消当前操作。")
            rc = 0
        except Exception as exc:
            print_fn(f"\n执行异常: {type(exc).__name__}: {exc}")
            rc = 1

        if rc != 0:
            print_fn(f"\n(本次操作退出码: {rc})")
        if pause:
            try:
                input_fn("\n按 Enter 返回主菜单...")
            except (EOFError, KeyboardInterrupt):
                return 0
