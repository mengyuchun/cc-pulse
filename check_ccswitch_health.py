#!/usr/bin/env python3
"""
CC-Pulse — cc-switch 供应商健康检测脚本（独立运行，不改 cc-switch 任何东西）

直接只读 cc-switch 的 SQLite 数据库，对每个供应商的上游 endpoint 发一次
真实问题探测请求，收集状态码、完整错误信息和实际回答内容。

特点:
  - 不依赖 cc-switch 运行状态（只读它的数据库）
  - 不依赖 CLIProxyAPI（直接打上游）
  - 批量并发 + 完整错误信息透传
  - 认证头按 cc-switch 配置走（AUTH_TOKEN→Bearer，API_KEY→x-api-key）
  - 路径拼接不去重（和真实 Claude Code 一致，muyuan.do/v1 → /v1/v1/messages）
  - 模型多档回退：haiku→sonnet→opus→fable→default，每档结果都报告
  - 探测真实问题：发 "2+3=?" 校验能否正确回答，而非只测连通

用法:
    python check_ccswitch_health.py
    python check_ccswitch_health.py --failover-only --workers 8
    python check_ccswitch_health.py --type claude --timeout 60
"""

import argparse
import http.client
import json
import random
import re
import sqlite3
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TypedDict

from ccpulse_archive import parse_since, run_trend
from ccpulse_env import run_env_check
from ccpulse_output import (
    _c,
    _output_stream,
    _pad,
    _sanitize_for_terminal,
    _say_colored,
    say,
)
from ccpulse_net import (  # noqa: F401
    ErrorCategory,
    HttpResponse,
    STREAM_DONE_MARKERS,
    StreamEvent,
    _category_from_status,
    _error_category_for_urlerror,
    _http_request,
    _is_tls_error,
    _parse_retry_after,
    _process_sse_event,
    _read_httperror_body,
    _sanitize_display,
    _sanitize_raw_body,
    _sse_event_to_dict,
    classify_error,
    create_ssl_context,
    parse_sse_lines,
)
import ccpulse_net as _ccpulse_net  # noqa: E402

import ccpulse_probe  # noqa: E402
from ccpulse_probe import (  # noqa: F401
    EXPECTED_ANSWER,
    FetchModelsResult,
    HistoryResult,
    InspectDimensionResult,
    ModelTier,
    PROBE_MAX_TOKENS,
    PROBE_PROMPTS,
    PROBE_QUESTION,
    ProbeStreamResult,
    ProbeTierResult,
    Protocol,
    Provider,
    STEALTH_JITTER_MAX,
    STEALTH_JITTER_MIN,
    STEALTH_MAX_WORKERS,
    TIER_ENV_KEYS,
    TIER_ORDER,
    _CLAUDE_CLI_VERSION_CACHE,
    _CLAUDE_VERSION_LOCK,
    _COMPARE_DEFAULT_INCLUDE,
    _DEFAULT_CLAUDE_CLI_VERSION,
    _INSPECT_DEFAULT_INCLUDE,
    _MODEL_SUFFIX_PATTERNS,
    _MODEL_SUFFIX_REGEX,
    _PROBE_PNG_B64,
    _STAINLESS_PACKAGE_VERSION,
    _THINKING_PRONE_RE,
    _TOOL_DESC,
    _TOOL_NAME,
    _answer_correct,
    _build_context_filler,
    _build_proto_payload,
    _build_proto_url,
    _claude_cli_version,
    _claude_code_headers,
    _collect_models_for_probe,
    _detect_claude_cli_version,
    _drain_non_sse_stream,
    _drain_sse_stream,
    _inspect_model_consistency,
    _inspect_one_model,
    _inspect_text,
    _inspect_thinking,
    _inspect_verdict,
    _is_rate_limited,
    _is_thinking_prone_model,
    _normalize_model_id,
    _probe_one_model,
    _probe_tools,
    _probe_vision,
    _resolve_protocol,
    _response_has_thinking_signal,
    _status_badge,
    _user_agent,
    build_auth_headers,
    build_probe_request,
    compare_models,
    detect_protocol,
    extract_answer,
    extract_model_ids,
    extract_usage,
    fetch_models,
    probe,
    probe_context_smoke,
    probe_model_metadata,
    probe_stream,
    probe_tier,
    rebuild_provider_for_inspect,
)

# 拆分后子模块的私有名经 module __getattr__ 透传，保证 importlib.util
# 加载本文件时外部仍可按原名访问 net/probe 层符号。

import ccpulse_store  # noqa: E402
from ccpulse_store import (  # noqa: F401
    _day_key,
    _open_ro,
    _percentile,
    _row_is_fail,
    _sparkline,
    _table_exists,
    analyze_by_day,
    analyze_by_model,
    analyze_by_provider_day,
    analyze_provider_deep,
    classify_log_error,
    _fmt_ts,
    load_provider_id_map,
    load_providers,
    parse_provider,
    query_analyze_raw,
    query_provider_health,
    query_proxy_logs,
    query_routing,
    query_stats,
    read_log_file_tail,
    resolve_provider_name,
    summarize_provider_history,
)

def __getattr__(name: str):  # noqa: D401
    for _m in (_ccpulse_net, ccpulse_probe, ccpulse_store):
        try:
            return getattr(_m, name)
        except AttributeError:
            continue
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None

DB_PATH = str(Path.home() / ".cc-switch" / "cc-switch.db")



def run_list_models(args, providers, say) -> int:
    """拉取每个供应商的 /v1/models 模型目录（不进行健康探测）。"""
    if getattr(args, "current_only", False):
        scope = "仅当前激活"
    elif getattr(args, "failover_only", False):
        scope = "故障转移队列"
    else:
        scope = "全部"
    args_type = getattr(args, "type", "claude")
    say(f"从 {args.db} 加载 {len(providers)} 个供应商 ({scope})")
    say(
        f"拉取模型列表: GET /v1/models  并发: {args.workers}  超时: {args.timeout}s (type={args_type})\n"
    )
    results = []
    _ua = getattr(args, "user_agent", None)
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {
            ex.submit(fetch_models, p, args.timeout, args.skip_tls_verify, _ua): p
            for p in providers
        }
        for i, fut in enumerate(as_completed(futs), 1):
            try:
                r = fut.result()
            except Exception as exc:  # noqa: BLE001 - 防御性兜底
                p = futs[fut]
                r = {
                    "name": getattr(p, "name", "?"),
                    "base_url": getattr(p, "base_url", "?"),
                    "status": 0,
                    "elapsed": 0,
                    "error": f"内部异常: {type(exc).__name__}: {exc}",
                    "error_category": ErrorCategory.UNKNOWN.value,
                    "models": [],
                }
            results.append(r)
            if r["status"] == 200:
                say(
                    f"[{i:>2}/{len(providers)}] ✅ {r['name'][:24]:24} {len(r['models'])} 个模型"
                )
            else:
                say(
                    f"[{i:>2}/{len(providers)}] ❌ {r['name'][:24]:24} [{r['status']}] {r['error'][:40]}"
                )

    ok = [r for r in results if r["status"] == 200]
    say(f"\n{'=' * 60}")
    say(f"完成: ✅ {len(ok)} 个供应商返回模型列表  共 {len(results)} 个")
    say(f"{'=' * 60}")
    for r in ok:
        say(f"\n■ {r['name']}  ({len(r['models'])} 个模型)  {r['base_url']}")
        for mid in r["models"]:
            say(f"    {mid}")
    fail = [r for r in results if r["status"] != 200]
    if fail:
        say(f"\n未返回模型列表的供应商（{len(fail)} 个）:")
        for r in fail:
            say(f"  ❌ {r['name'][:24]:24} [{r['status']}] {r['error']}")

    # --probe / --deep：拉列表后逐模型探测（轻量或深度 12356）
    probe_reports = []
    if getattr(args, "probe", False) or getattr(args, "deep", False):
        deep = getattr(args, "deep", False)
        source = getattr(args, "source", "listed")
        result_by_name = {r["name"]: r for r in results}
        say(f"\n{'=' * 60}")
        say(
            f"探测模式: {'深度(text/streaming/metadata/thinking/tools)' if deep else '轻量(text)'}"
            f"  来源: {source}"
        )
        say(f"{'=' * 60}")
        for p in providers:
            fr = result_by_name.get(p.name, {"status": 0, "models": []})
            model_ids = _collect_models_for_probe(p, fr, source)
            if not model_ids:
                say(f"\n■ {p.name}: 无可探测模型（source={source}）")
                continue
            say(f"\n■ {p.name}  待探测 {len(model_ids)} 个模型")
            model_reports = []
            for j, mid in enumerate(model_ids, 1):
                rep = _probe_one_model(p, mid, args, deep)
                model_reports.append(rep)
                tr = rep["text"]
                badge = (
                    "✅"
                    if tr["status"] == "pass"
                    else ("⚠" if tr["status"] == "fail" else "❌")
                )
                line = (
                    f"  [{j:>2}/{len(model_ids)}] {badge} {mid[:32]:32} "
                    f"[{tr['http_status']}] {tr['elapsed_seconds']}s"
                )
                if deep:
                    sm = rep["streaming"].get("status")
                    tk = rep["thinking"].get("verdict", "?")
                    to = rep["tools"].get("protocol_support", "?")
                    line += f"  stream:{sm} think:{tk} tools:{to}"
                else:
                    line += f'  "{tr["answer"][:20]}"'
                say(line)
            probe_reports.append(
                {
                    "provider": p.name,
                    "app_type": p.app_type,
                    "base_url": p.base_url,
                    "models": model_reports,
                }
            )

    if getattr(args, "json", False):
        # 给每个 provider 附配置档位（带档位名），供启动器标注「[haiku 档位]」
        by_name = {p.name: p for p in providers}
        for r in results:
            r["configured_models"] = (
                [{"tier": t.tier, "model": t.model} for t in by_name[r["name"]].tiers]
                if r["name"] in by_name
                else []
            )
        report = {
            "schema_version": 1,
            "command": "list-models",
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "scope": scope,
            "type": args_type,
            "providers": results,
        }
        if getattr(args, "probe", False) or getattr(args, "deep", False):
            report["probe"] = {
                "deep": getattr(args, "deep", False),
                "source": getattr(args, "source", "listed"),
                "reports": probe_reports,
            }
        print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    return 0


def _archive_check_results(args, providers, results) -> None:
    """把 check 探测结果追加到本地归档（供 trend 跨次聚合）。

    每条 provider 记录：ts / provider / model / status / latency / ttft / error_category。
    失败不阻断探测：IO 或数据异常全部吞掉。
    """
    try:
        from ccpulse_archive import append_record, archive_path, trim_archive

        path = archive_path(getattr(args, "archive", None))
        now = int(time.time())
        for r in results:
            ok = r.get("overall_ok")
            bt = r.get("best_tier")
            attempt = None
            if ok and bt:
                attempt = next((a for a in r["attempts"] if a["tier"] == bt), None)
            if attempt is None and r["attempts"]:
                attempt = r["attempts"][0]
            record = {
                "ts": now,
                "command": "check",
                "provider": r.get("name", "?"),
                "model": (attempt or {}).get("model", "?") or "?",
                "status": "ok" if ok else "fail",
                "latency": (attempt or {}).get("elapsed"),
                "ttft": (attempt or {}).get("ttft_seconds"),
                "error_category": (attempt or {}).get("error_category"),
            }
            append_record(path, record)
        # 归档轮转：超 5MB 且 >10000 条时保留最新
        trim_archive(path)
    except Exception as e:  # noqa: BLE001 - 归档失败不影响健康检测
        sys.stderr.write(f"警告: check 结果归档失败: {e}\n")


def run_health_check(args, providers, say) -> int:
    """对每个供应商按档位回退顺序进行真实问题探测。

    进度策略：
      - 每个档位尝试结束立刻打印一行（on_attempt）
      - 每个供应商全部档位结束后再打印汇总行
      - say() 默认 flush，配合启动器 -u，避免管道块缓冲导致「全部结束才显示」
    """
    if getattr(args, "current_only", False):
        scope = "仅当前激活"
    elif getattr(args, "failover_only", False):
        scope = "故障转移队列"
    else:
        scope = "全部"
    _stealth = getattr(args, "stealth", False)
    _workers = min(args.workers, STEALTH_MAX_WORKERS) if _stealth else args.workers
    say(f"从 {args.db} 加载 {len(providers)} 个供应商 ({scope})")
    _mode = f"  隐身: 开(并发≤{STEALTH_MAX_WORKERS}+随机延迟)" if _stealth else ""
    say(f"并发: {_workers}  超时: {args.timeout}s{_mode}")
    say(f"探测问题: 问题池随机抽取（{len(PROBE_PROMPTS)} 条，答案宽松匹配）")
    say(f"档位回退: {' → '.join(TIER_ORDER)}  认证: 按配置  路径: 不去重")
    say("进度: 每档完成立即显示，供应商完成显示汇总\n")

    results = []
    health_started = time.time()
    _notes_map = {
        p.name: (p.notes or "").strip() for p in providers
    }  # 供应商名→运营备注
    _mt = getattr(args, "probe_max_tokens", PROBE_MAX_TOKENS)
    _dt = not getattr(args, "probe_enable_thinking", False)
    _ua = getattr(args, "user_agent", None)
    _sv = getattr(args, "stainless_version", None)

    def _on_attempt(p: Provider, r: dict) -> None:
        # 档位级增量：多线程下可能交错，但每行原子且带供应商名
        st = r.get("status", 0)
        if st == 200 and r.get("correct"):
            say(
                f"  · {p.name[:22]:22} {r['tier']:6} [ok] {r.get('elapsed', 0)}s "
                f'回答:"{r.get("answer", "")}"'
            )
        elif st == 200:
            say(
                f"  · {p.name[:22]:22} {r['tier']:6} [答案不符] {r.get('elapsed', 0)}s "
                f'"{r.get("answer", "")}"'
            )
        else:
            err = (r.get("error") or "")[:40]
            say(
                f"  · {p.name[:22]:22} {r['tier']:6} [{st}] {r.get('elapsed', 0)}s {err}"
            )

    output_stream = _output_stream.get()

    def _probe_in_parent_context(p: Provider) -> dict:
        # ThreadPoolExecutor 不会自动复制上下文（只有 ProcessPoolExecutor 会），
        # 这里显式把父线程的输出流传给 worker，否则 _on_attempt 的 say() 会落到 stdout。
        token = _output_stream.set(output_stream)
        try:
            return probe(
                p,
                args.timeout,
                args.skip_tls_verify,
                _mt,
                _dt,
                _ua,
                _on_attempt,
                stainless_version=_sv,
                stealth=_stealth,
                max_retries=getattr(args, "retry", 0),
            )
        finally:
            _output_stream.reset(token)

    with ThreadPoolExecutor(max_workers=_workers) as ex:
        futs = {ex.submit(_probe_in_parent_context, p): p for p in providers}
        for i, fut in enumerate(as_completed(futs), 1):
            try:
                r = fut.result()
            except Exception as exc:  # noqa: BLE001 - 防御性兜底，防 worker 异常穿透
                p = futs[fut]
                r = {
                    "name": p.name,
                    "type": p.app_type,
                    "base_url": p.base_url,
                    "auth_mode": p.auth_mode,
                    "overall_ok": False,
                    "best_tier": None,
                    "attempts": [
                        {
                            "tier": "?",
                            "model": "?",
                            "status": 0,
                            "elapsed": 0,
                            "error": f"内部异常: {type(exc).__name__}: {exc}",
                            "error_category": ErrorCategory.UNKNOWN.value,
                        }
                    ],
                }
            results.append(r)
            icon = "✅" if r["overall_ok"] else "❌"
            if r["overall_ok"]:
                bt = r["best_tier"]
                # 找到成功那档的答案
                ans = next((a["answer"] for a in r["attempts"] if a["tier"] == bt), "")
                say(
                    f'[{i:>2}/{len(providers)}] {icon} {r["name"][:24]:24} ✓{bt} 回答:"{ans}"'
                )
            else:
                # 列出每个失败档位的简短结果
                fails = " | ".join(
                    f"{a['tier']}:{a['status']}({a['error'][:30]})"
                    for a in r["attempts"]
                )
                _nt = _notes_map.get(r["name"], "")
                _sfx = f"  notes:{_nt[:40]}" if _nt else ""
                say(
                    f"[{i:>2}/{len(providers)}] {icon} {r['name'][:24]:24} {fails}{_sfx}"
                )
            if getattr(args, "with_history", False):
                try:
                    say(
                        format_history_sidebar(
                            args.db,
                            r["name"],
                            since=getattr(args, "history_since", "24h") or "24h",
                        )
                    )
                except Exception as e:  # noqa: BLE001 - 历史附加信息不能中断健康检测
                    say(f"  history: 读取失败 ({type(e).__name__}: {e})")
    ok = [r for r in results if r["overall_ok"]]
    fail = [r for r in results if not r["overall_ok"]]
    say(f"\n{'=' * 60}")
    say(
        f"完成: ✅ {len(ok)} 可用(能正确回答)  ❌ {len(fail)} 不可用  共 {len(results)} 个"
    )
    say(f"{'=' * 60}")

    if fail:
        say("\n不可用详情（每档尝试结果）:")
        for r in fail:
            _nt = _notes_map.get(r["name"], "")
            say(f"  ❌ {r['name'][:24]:24} {r['base_url']}  [auth:{r['auth_mode']}]")
            if _nt:
                say(f"      notes: {_nt[:60]}")
            for a in r["attempts"]:
                say(
                    f"      {a['tier']:8} {a['model']:28} [{a['status']}] {a['elapsed']}s"
                )
                if a["error"]:
                    say(f"               → {a['error']}")

    if ok:
        say("\n可用详情:")
        for r in ok:
            bt = r["best_tier"]
            a = next(x for x in r["attempts"] if x["tier"] == bt)
            say(
                f'  ✅ {r["name"][:24]:24} 档位:{bt:8} {a["model"]:28} {a["elapsed"]}s 回答:"{a["answer"]}"'
            )

    # 健康供应商推荐：只读不改库，输出可消费的切换提示
    if ok and fail:
        say("\n💡 推荐切换到健康供应商（只读提示，不会自动改库）:")
        rec = min(ok, key=lambda x: next(a["elapsed"] for a in x["attempts"] if a["tier"] == x["best_tier"]))
        rec_a = next(a for a in rec["attempts"] if a["tier"] == rec["best_tier"])
        say(f"  首选: {rec['name']}（{rec['best_tier']} 档位 {rec_a['model']}，{rec_a['elapsed']}s）")
        say(f"  → 在 cc-switch 切换到「{rec['name']}」即可")

    # JSON 模式：最后输出结构化报告到 stdout
    if getattr(args, "json", False):
        # 结果按 providers 的输入顺序（sort_index）稳定排序，
        # 避免 as_completed 的完成序导致 JSON 每次不同；type 纳入 key，
        # 防止 --type all 下同名供应商互相覆盖。
        order = {(p.app_type, p.name): i for i, p in enumerate(providers)}
        results_sorted = sorted(
            results,
            key=lambda r: order.get((r.get("type"), r.get("name")), 1_000_000),
        )
        json_report = {
            "schema_version": 2,
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "db_path": args.db,
            "scope": (
                "current"
                if getattr(args, "current_only", False)
                else "failover"
                if getattr(args, "failover_only", False)
                else "all"
            ),
            "type": getattr(args, "type", "claude"),
            "probe_question": "randomized",
            "probe_pool_size": len(PROBE_PROMPTS),
            "stealth": _stealth,
            "tier_order": TIER_ORDER,
            "elapsed_seconds": round(time.time() - health_started, 2),
            "summary": {
                "total": len(results),
                "available": len(ok),
                "unavailable": len(fail),
                "available_ratio": round(len(ok) / len(results), 4) if results else 0,
            },
            "providers": results_sorted,
        }
        print(json.dumps(json_report, ensure_ascii=False, indent=2), flush=True)

    # 归档：每次 check 探测结果追加一行 JSONL（供 trend 跨次聚合），失败不阻断
    _archive_check_results(args, providers, results)

    # 告警阈值：可用率低于阈值时输出告警（cron 可据此触发）
    alert_threshold = getattr(args, "alert_threshold", None)
    if alert_threshold is not None and results:
        ratio = len(ok) / len(results)
        if ratio < alert_threshold:
            say(
                f"⚠ 告警: 可用率 {ratio * 100:.0f}% 低于阈值 {alert_threshold * 100:.0f}%",
            )
    return 0 if ok else 1


def resolve_inspect_target(args, providers, say) -> tuple:
    """解析 inspect 子命令的目标 (Provider, model_id, error_message)。

    返回 (provider, model_id, None) 成功；或 (None, None, error_message) 失败。
    不做实际探测，只做"目标是否可识别"的判断。
    """
    name = args.provider
    model = args.model

    # 1. 找到供应商
    p = next((x for x in providers if x.name == name), None)
    if p is None:
        return None, None, f"未找到供应商: {name!r}（当前 type={args.type}）"

    # 2. 按 source 处理模型
    if args.source == "configured":
        # 精确匹配 raw_model 或 stripped model
        for t in p.tiers:
            if t.raw_model == model or t.model == model:
                # 默认用 stripped model（与 check 子命令一致，上游可能拒带 [1M] 后缀）；
                # --keep-suffix 时保留原始 raw_model
                return p, (t.raw_model if args.keep_suffix else t.model), None
        available = [t.raw_model for t in p.tiers] or ["(无档位)"]
        return (
            None,
            None,
            (f"供应商 {name!r} 未配置模型 {model!r}；可用档位: {', '.join(available)}"),
        )

    if args.source == "manual":
        # 强制使用用户提供的字面值；--keep-suffix 决定是否去后缀
        raw = model
        clean = raw if args.keep_suffix else re.sub(r"\[.*?\]$", "", raw)
        return p, clean, None

    if args.source == "listed":
        # 调一次 /v1/models，找到则复用供应商
        r = fetch_models(
            p,
            args.timeout,
            args.skip_tls_verify,
            user_agent=getattr(args, "user_agent", None),
            max_retries=getattr(args, "retry", 0),
        )
        if r["status"] != 200:
            return (
                None,
                None,
                f"拉取 /v1/models 失败: [{r['status']}] {r['error'][:80]}",
            )
        if model not in r["models"]:
            preview = ", ".join(r["models"][:10])
            return (
                None,
                None,
                (f"供应商 {name!r} /v1/models 中未列出 {model!r}；前 10 个: {preview}"),
            )
        raw = model
        clean = raw if args.keep_suffix else re.sub(r"\[.*?\]$", "", raw)
        return p, clean, None

    return None, None, f"未知 source: {args.source}"


def _parse_include(raw: str | None, default: str) -> set[str]:
    """解析 --include；None/空 → default。"""
    src = default if raw is None else raw
    out = {x.strip() for x in src.split(",") if x.strip()}
    return out or {x.strip() for x in default.split(",") if x.strip()}


def _parse_compare_targets(spec: str) -> list[tuple[str, str]]:
    """解析 --compare 规格字符串 → [(provider, model), ...]。

    格式：逗号分隔的 'provider/model' 对。
    provider 名或 model 名本身含 / 时，最后一个 / 之后是 model，之前是 provider。
    """
    out = []
    for raw in (spec or "").split(","):
        part = raw.strip()
        if not part:
            continue
        if "/" not in part:
            raise ValueError(f"--compare 项缺少 '/'：{part!r}（应为 provider/model）")
        provider, model = part.rsplit("/", 1)
        provider, model = provider.strip(), model.strip()
        if not provider or not model:
            raise ValueError(f"--compare 项 provider 或 model 为空：{part!r}")
        out.append((provider, model))
    if len(out) < 2:
        raise ValueError("--compare 至少需要 2 个 'provider/model' 目标")
    return out


def _run_inspect_compare(args, providers, say) -> int:
    """--compare: 对多个 (provider, model) 跑同一组维度，输出对齐对比报告。"""
    try:
        targets = _parse_compare_targets(args.compare)
    except ValueError as e:
        say(str(e))
        print(
            json.dumps(
                {
                    "schema_version": 1,
                    "command": "inspect-compare",
                    "error": str(e),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 2

    # 对比默认 text+streaming；只有用户显式 --include 才扩维度
    include = _parse_include(getattr(args, "include", None), _COMPARE_DEFAULT_INCLUDE)
    _mt = getattr(args, "probe_max_tokens", PROBE_MAX_TOKENS)
    _dt = not getattr(args, "probe_enable_thinking", False)
    _ua = getattr(args, "user_agent", None)
    human = (getattr(args, "output_format", None) == "human") or getattr(
        args, "human", False
    )
    delay = float(getattr(args, "probe_delay", 3.0) or 0)

    say(f"对比模式: {len(targets)} 个目标 · include={','.join(sorted(include))}")
    results = []
    for i, (pname, mid) in enumerate(targets, 1):
        say(f"\n[{i}/{len(targets)}] {pname} / {mid}")
        single = argparse.Namespace(**vars(args))
        single.provider = pname
        single.model = mid
        single.source = "manual"  # 对比跨供应商，不走 configured 校验
        single.all_models = False
        single.models = None
        single.compare = None
        p, model_id, err = resolve_inspect_target(single, providers, say)
        if p is None:
            say(f"  跳过: {err}")
            results.append(
                {
                    "provider": pname,
                    "model": mid,
                    "error": err,
                    "text": {
                        "status": "error",
                        "elapsed_seconds": 0,
                        "answer": "",
                        "correct": False,
                        "http_status": 0,
                        "error_category": "resolve_failed",
                        "error": err,
                    },
                    "streaming": {"status": "not_run"},
                    "summary": {"verdict": "unavailable"},
                }
            )
            continue
        inspect_p = rebuild_provider_for_inspect(p, model_id)
        report = _inspect_one_model(inspect_p, model_id, args, include, _mt, _dt, _ua)
        report["model_source"] = "manual"
        results.append(report)
        if i < len(targets) and delay > 0:
            time.sleep(delay)

    # 汇总对比表
    rows = []
    for r in results:
        t = r.get("text") or {}
        s = r.get("streaming") or {}
        rows.append(
            {
                "provider": r.get("provider"),
                "model": r.get("model"),
                "verdict": (r.get("summary") or {}).get("verdict", "error"),
                "text_status": t.get("status"),
                "text_elapsed": t.get("elapsed_seconds"),
                "answer": (t.get("answer") or "")[:40],
                "correct": t.get("correct"),
                "streaming_status": s.get("status"),
                "ttft": s.get("ttft_seconds"),
                "stream_elapsed": s.get("elapsed_seconds"),
                "error_category": t.get("error_category") or s.get("error_category"),
            }
        )

    out = {
        "schema_version": 1,
        "command": "inspect-compare",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "targets": [{"provider": p, "model": m} for p, m in targets],
        "include": sorted(include),
        "comparison": rows,
        "reports": results,
    }

    if human:
        print(_format_compare_human(rows), flush=True)
    else:
        print(json.dumps(out, ensure_ascii=False, indent=2), flush=True)

    # 退出码：全 healthy → 0；部分 → 1；全失败 → 3
    ok = sum(1 for r in rows if r["verdict"] in ("healthy", "skipped"))
    if ok == len(rows):
        return 0
    if ok > 0:
        return 1
    return 3


def _format_compare_human(rows: list) -> str:
    """对比报告人类可读表格。"""
    lines = ["=" * 72, "  对比报告", "=" * 72]
    hdr = f"{'#':>2}  {'provider':<18} {'model':<28} {'verdict':<12} {'text':>5} {'ttft':>6} {'ans'}"
    lines.append(hdr)
    lines.append("-" * 72)
    for i, r in enumerate(rows, 1):
        ttft = f"{r['ttft']:.2f}" if isinstance(r.get("ttft"), (int, float)) else "-"
        te = (
            f"{r['text_elapsed']:.2f}"
            if isinstance(r.get("text_elapsed"), (int, float))
            else "-"
        )
        ans = (r.get("answer") or "")[:20]
        lines.append(
            f"{i:>2}  {(r.get('provider') or '?'):<18} "
            f"{(r.get('model') or '?'):<28} "
            f"{(r.get('verdict') or '?'):<12} "
            f"{te:>5} {ttft:>6} {ans}"
        )
    lines.append("=" * 72)
    return "\n".join(lines)


def run_inspect(args, providers, say) -> int:
    """单一模型深度检测（7 维度：text/streaming/metadata/context/thinking/tools/vision）。"""
    # --format 与 --human 合并：--format human 优先，否则 --human 为 True 时 human，否则 json
    _fmt = getattr(args, "output_format", None)
    if _fmt is None:
        args.output_format = "human" if getattr(args, "human", False) else "json"
    if getattr(args, "compare", None):
        return _run_inspect_compare(args, providers, say)
    if not getattr(args, "provider", None):
        say("inspect 需要 --provider，或改用 --compare 'A/m1,B/m2'")
        print(
            json.dumps(
                {
                    "schema_version": 1,
                    "command": "inspect",
                    "error": "missing --provider",
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 2
    if getattr(args, "all_models", False) or getattr(args, "models", None):
        return _run_inspect_all(args, providers, say)
    p, model_id, err = resolve_inspect_target(args, providers, say)
    if p is None:
        say(err)
        print(
            json.dumps(
                {
                    "schema_version": 1,
                    "command": "inspect",
                    "error": err,
                    "provider": getattr(args, "provider", None),
                    "model": getattr(args, "model", None),
                    "source": getattr(args, "source", None),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 2

    inspect_p = rebuild_provider_for_inspect(p, model_id)
    protocol = detect_protocol(p)
    include = _parse_include(getattr(args, "include", None), _INSPECT_DEFAULT_INCLUDE)
    _mt = getattr(args, "probe_max_tokens", PROBE_MAX_TOKENS)
    _dt = not getattr(args, "probe_enable_thinking", False)
    _ua = getattr(args, "user_agent", None) or p.custom_user_agent

    if getattr(args, "with_metadata", False):
        say("提示: --with-metadata 已废弃，metadata 默认包含在 --include 中")

    # ---- 7 维度探测 ----
    text_result, text_raw = (None, None)
    if "text" in include:
        text_result, text_raw = _inspect_text(
            inspect_p,
            inspect_p.tiers[0],
            args.timeout,
            args.skip_tls_verify,
            max_tokens=_mt,
            disable_thinking=_dt,
            user_agent=_ua,
        )

    streaming_result = None
    if "streaming" in include:
        streaming_result = probe_stream(
            inspect_p,
            inspect_p.tiers[0],
            args.timeout,
            args.skip_tls_verify,
            ttft_timeout=getattr(args, "ttft_timeout", None),
            max_tokens=_mt,
            disable_thinking=_dt,
            user_agent=_ua,
        )

    metadata_result = {"status": "skipped"}
    if "metadata" in include:
        metadata_result = probe_model_metadata(
            inspect_p,
            re.sub(r"\[.*?\]$", "", model_id),
            args.timeout,
            args.skip_tls_verify,
            user_agent=_ua,
        )

    context_result = {"status": "skipped"}
    if "context" in include or (
        getattr(args, "include", None) is None and "metadata" in include
    ):
        has_declared = (
            metadata_result.get("declared_context_window") is not None
            and metadata_result.get("status") == "available"
        )
        if not has_declared:
            _ctx = {"512k": 524288, "1m": 1048576}.get(
                getattr(args, "probe_context", "512k"), 524288
            )
            context_result = probe_context_smoke(
                inspect_p,
                model_id,
                _ctx,
                args.timeout,
                args.skip_tls_verify,
                user_agent=_ua,
            )

    thinking_result = {"status": "skipped"}
    if "thinking" in include:
        thinking_result = _inspect_thinking(
            inspect_p,
            inspect_p.tiers[0],
            text_raw,
            args.timeout,
            args.skip_tls_verify,
            max_tokens=_mt,
            user_agent=_ua,
        )

    tools_result = {"status": "skipped"}
    if "tools" in include:
        tools_result = _probe_tools(
            inspect_p, model_id, args.timeout, args.skip_tls_verify, _ua
        )

    vision_result = {"status": "skipped"}
    if "vision" in include:
        vision_result = _probe_vision(
            inspect_p, model_id, args.timeout, args.skip_tls_verify, _ua
        )

    # ---- 汇总 ----
    if text_result and text_result["status"] == "pass":
        protocol["confidence"] = "confirmed"

    responded_model = (streaming_result or {}).get("response_model")
    model_consistency = _inspect_model_consistency(model_id, responded_model, include)

    usage = {
        "present": False,
        "input_tokens": None,
        "output_tokens": None,
        "source": None,
        "missing_fields": ["input_tokens", "output_tokens"],
    }
    if text_raw and text_raw.get("usage"):
        usage = text_raw["usage"]

    verdict, anomaly, recommended = _inspect_verdict(
        text_result, model_consistency, thinking_result, tools_result
    )

    report = {
        "schema_version": 1,
        "command": "inspect",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "provider": inspect_p.name,
        "model": model_id,
        "model_source": args.source,
        "base_url": inspect_p.base_url,
        "auth_mode": inspect_p.auth_mode,
        "custom_user_agent": p.custom_user_agent,  # 原始 Provider（未 rebuild）
        "notes": p.notes,  # 原始 Provider 运营备注
        "protocol": protocol,
        "text": text_result,
        "streaming": streaming_result
        if streaming_result is not None
        else {"status": "not_run"},
        "metadata": metadata_result,
        "context": context_result,
        "thinking": thinking_result,
        "tools": tools_result,
        "vision": vision_result,
        "model_consistency": model_consistency,
        "usage": usage,
        "summary": {
            "verdict": verdict,
            "model_routing_anomaly": anomaly,
            "recommended_actions": recommended,
        },
    }
    if getattr(args, "with_history", False):
        try:
            since = getattr(args, "history_since", "24h") or "24h"
            report["history"] = summarize_provider_history(
                args.db, inspect_p.name, since_ts=parse_since(since)
            )
            report["history_since"] = since
        except Exception as e:  # noqa: BLE001 - 历史附加信息不能中断 inspect
            report["history"] = {"error": f"{type(e).__name__}: {e}"}
    if args.output_format == "human" or args.human:
        text = format_inspect_human(report)
        if report.get("history") and not report["history"].get("error"):
            h = report["history"]
            since = report.get("history_since", "24h")
            rate = f"{h.get('success_rate', 0) * 100:.0f}%"
            text += (
                f"\n  history({since}): 请求{h.get('total')} 成功{rate} "
                f"失败{h.get('fail')} 主因={h.get('top_fail_category') or '-'} "
                f"路由≠{h.get('mismatch_rate', 0) * 100:.0f}%"
            )
        elif report.get("history", {}).get("error"):
            text += f"\n  history: {report['history']['error']}"
        say(text)
    else:
        print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)

    # 归档：inspect 结果追加一行（供 trend 跨次聚合），失败不阻断
    try:
        from ccpulse_archive import append_record, archive_path

        lat = None
        ttft = None
        if streaming_result and streaming_result.get("ttft_seconds") is not None:
            ttft = streaming_result["ttft_seconds"]
        if text_result and text_result.get("elapsed") is not None:
            lat = text_result.get("elapsed")
        append_record(
            archive_path(getattr(args, "archive", None)),
            {
                "ts": int(time.time()),
                "command": "inspect",
                "provider": inspect_p.name,
                "model": model_id,
                "status": "ok" if verdict in ("healthy", "skipped") else "fail",
                "latency": lat,
                "ttft": ttft,
                "error_category": (text_result or {}).get("error_category"),
            },
        )
    except Exception as e:  # noqa: BLE001 - 归档失败不影响 inspect
        sys.stderr.write(f"警告: inspect 结果归档失败: {e}\n")

    return 0 if verdict in ("healthy", "skipped") else 1


def _run_inspect_all(args, providers, say) -> int:
    """--all-models / --models: 对一 provider 下多个模型逐个跑 inspect。"""
    name = args.provider
    p = next((x for x in providers if x.name == name), None)
    if p is None:
        say(f"未找到供应商: {name!r}（当前 type={args.type}）")
        return 2

    configured = [t.model for t in p.tiers]
    listed = []

    # --models 优先：用户直接指定模型 ID 列表
    if getattr(args, "models", None):
        models = [m.strip() for m in args.models.split(",") if m.strip()]
        models = list(dict.fromkeys(models))  # 去重保序
        say(f"批量 inspect: {name} · {len(models)} 个模型 (用户指定)")
    else:
        src = getattr(args, "source", "configured")
        if src in ("listed", "both"):
            _ua = getattr(args, "user_agent", None)
            r = fetch_models(
                p,
                args.timeout,
                args.skip_tls_verify,
                user_agent=_ua,
                max_retries=getattr(args, "retry", 0),
            )
            if r["status"] == 200:
                listed = r["models"]
            else:
                say(
                    f"拉 /v1/models 失败: [{r['status']}] {r['error'][:80]}；用 configured 降级"
                )
        if src == "configured":
            models = list(dict.fromkeys(configured))
        elif src == "listed":
            models = listed or list(dict.fromkeys(configured))
        else:  # both
            models = list(dict.fromkeys(configured + listed))
        say(f"批量 inspect: {name} · {len(models)} 个模型 (source={src})")

    if not models:
        say(f"供应商 {name!r} 无可用模型")
        return 2

    delay = float(getattr(args, "probe_delay", 3.0) or 0)
    max_retries = int(getattr(args, "max_retries", 1) or 0)
    human = (getattr(args, "output_format", None) == "human") or getattr(
        args, "human", False
    )
    say(
        f"模型间延迟 {delay}s · 429 重试 {max_retries} 次 · 输出={'human' if human else 'json'}\n"
    )

    include = _parse_include(getattr(args, "include", None), _INSPECT_DEFAULT_INCLUDE)
    _mt = getattr(args, "probe_max_tokens", PROBE_MAX_TOKENS)
    _dt = not getattr(args, "probe_enable_thinking", False)
    _ua = getattr(args, "user_agent", None)

    fail_count = 0
    reports = []
    for i, m in enumerate(models, 1):
        say(f"\n{'=' * 60}")
        say(f"[{i}/{len(models)}] {m}")
        say(f"{'=' * 60}")

        single_args = argparse.Namespace(**vars(args))
        single_args.model = m
        single_args.all_models = False
        single_args.source = "manual"  # 批量不走 configured 校验
        p_single, model_id, err = resolve_inspect_target(single_args, providers, say)
        if p_single is None:
            say(f"错误: {err}")
            fail_count += 1
            reports.append(
                {
                    "provider": name,
                    "model": m,
                    "error": err,
                    "summary": {"verdict": "unavailable"},
                }
            )
            if i < len(models) and delay > 0:
                time.sleep(delay)
            continue

        inspect_p = rebuild_provider_for_inspect(p_single, model_id)
        # 429 重试
        attempt = 0
        while True:
            report = _inspect_one_model(
                inspect_p, model_id, args, include, _mt, _dt, _ua
            )
            report["model_source"] = getattr(args, "source", "manual")
            rate_hit = any(
                _is_rate_limited(report.get(k))
                for k in ("text", "streaming", "metadata", "tools", "vision")
            )
            if rate_hit and attempt < max_retries:
                attempt += 1
                wait = delay * attempt if delay > 0 else 3.0 * attempt
                say(f"  ⚠ 429 rate_limit，{wait:.1f}s 后重试 ({attempt}/{max_retries})")
                time.sleep(wait)
                continue
            break
        # 把限速重试次数写进 report，便于运维事后追溯
        report["rate_limit_retries"] = attempt
        if attempt > 0:
            report.setdefault("summary", {})["rate_limited"] = True

        reports.append(report)
        if human:
            say(format_inspect_human(report))
        else:
            # 流式 JSON：每个模型一行 NDJSON，便于实时消费
            print(json.dumps(report, ensure_ascii=False), flush=True)

        if report.get("summary", {}).get("verdict") not in ("healthy", "skipped"):
            fail_count += 1

        if i < len(models) and delay > 0:
            time.sleep(delay)

    say(f"\n{'=' * 60}")
    say(f"批量检测完成: {len(models)} 个模型, {fail_count} 个失败")
    say(f"{'=' * 60}")
    # human 模式末尾再打一张汇总表
    if human and reports:
        print(f"\n{'#':>3}  {'模型':<42}  verdict")
        print("-" * 60)
        for i, r in enumerate(reports, 1):
            v = (r.get("summary") or {}).get("verdict", "error")
            m = r.get("model", "?")
            print(f"{i:>3}  {m[:40]:<42}  {v}")
    # 退出码粒度：0 全成功 / 1 部分失败 / 3 全部失败（仅批量模式）
    if fail_count == 0:
        return 0
    if fail_count < len(models):
        return 1  # 部分失败
    return 3  # 全部失败


def format_inspect_human(r: dict) -> str:
    """把 inspect 报告格式化为人类可读文本。"""
    lines = []
    lines.append("=" * 60)
    lines.append(f"  Provider:  {r['provider']}")
    lines.append(f"  Model:     {r['model']} ({r['model_source']})")
    lines.append(
        f"  Protocol:  {r['protocol']['detected']} · {r['protocol']['confidence']}"
    )
    if r.get("custom_user_agent"):
        lines.append(f"  UA:        {r['custom_user_agent']}")
    if r.get("notes"):
        lines.append(f"  Notes:     {r['notes']}")
    lines.append("=" * 60)
    lines.append("")

    # [1] 文本 + usage
    if r.get("text"):
        t = r["text"]
        if t["status"] == "pass":
            lines.append("[1/7] 文本探测")
            lines.append(f"  状态：✅ pass · {t['elapsed_seconds']}s")
            lines.append(f'  答案："{t["answer"]}" · 正确')
        elif t["status"] == "fail":
            lines.append("[1/7] 文本探测")
            lines.append(f"  状态：⚠ fail · {t['elapsed_seconds']}s")
            lines.append(f'  答案："{t["answer"]}" · 不正确')
        else:
            lines.append("[1/7] 文本探测")
            lines.append(
                f"  状态：❌ error · {t['elapsed_seconds']}s · [{t['error_category']}]"
            )
            if t["error"]:
                lines.append(f"  错误：{t['error']}")
    else:
        lines.append("[1/7] 文本探测 · skipped")
    u = r.get("usage") or {}
    if u.get("present"):
        lines.append(
            f"  usage：in={u.get('input_tokens')} out={u.get('output_tokens')}"
        )
    else:
        lines.append("  usage：未返回 / 未解析")

    # [2] 流式
    lines.append("")
    s = r.get("streaming") or {}
    if s.get("status") in ("pass", "fail", "error"):
        ttft = s.get("ttft_seconds")
        ttft_str = f"TTFT {ttft}s" if ttft is not None else "无首 token"
        lines.append("[2/7] 流式探测")
        if s["status"] == "pass":
            lines.append(f"  状态：✅ pass · {ttft_str} · 总 {s['elapsed_seconds']}s")
        elif s["status"] == "fail":
            lines.append(f"  状态：⚠ fail · {ttft_str} · 总 {s['elapsed_seconds']}s")
        else:
            lines.append(f"  状态：❌ error · [{s.get('error_category')}]")
            if s.get("error"):
                lines.append(f"  错误：{s['error']}")
        if s.get("content_type"):
            lines.append(f"  Content-Type: {s['content_type']}")
        if s.get("response_model"):
            lines.append(f"  响应模型: {s['response_model']}")
        if s.get("event_count") is not None:
            lines.append(f"  事件数: {s['event_count']}")
    else:
        lines.append(f"[2/7] 流式探测 · {s.get('status', 'not_run')}")

    # [3] 模型路由
    lines.append("")
    m = r.get("model_consistency") or {}
    if m.get("responded"):
        warn = m.get("warning") or ""
        lines.append("[3/7] 模型路由比对")
        lines.append(f"  请求：{m['requested']}")
        lines.append(f"  响应：{m['responded']}")
        lines.append(f"  匹配：{m.get('match')} {warn}")
    else:
        lines.append(f"[3/7] 模型路由比对 · {m.get('match', 'not_run')}")

    # [4] 元数据 / 上下文
    lines.append("")
    md = r.get("metadata") or {}
    ctx = r.get("context") or {}
    if md.get("status") in ("available", "unavailable"):
        lines.append("[4/7] 模型元数据")
        if md["status"] == "available":
            cwin = md.get("declared_context_window")
            mout = md.get("max_output_tokens")
            if cwin:
                lines.append(f"  声明上下文窗口：{cwin:,} tokens（供应商声明，非实测）")
            if mout:
                lines.append(f"  声明最大输出：{mout:,} tokens")
            caps = md.get("capabilities") or {}
            if caps:
                true_caps = [k for k, v in caps.items() if v]
                if true_caps:
                    lines.append(f"  能力：{', '.join(true_caps)}")
            if not cwin:
                lines.append("  无声明窗口 → 触发上下文冒烟")
        else:
            lines.append(f"  状态：unavailable · [{md.get('error_category')}]")
            if md.get("error"):
                lines.append(f"  错误：{md['error'][:120]}")
    else:
        lines.append(f"[4/7] 模型元数据 · {md.get('status', 'not_run')}")
    if ctx.get("status") and ctx.get("status") != "skipped":
        lines.append(
            f"  上下文冒烟：{ctx.get('status')} · chars≈{ctx.get('approx_input_chars')} · "
            f"{ctx.get('token_estimate', '')}"
        )
        if ctx.get("error"):
            lines.append(f"  冒烟错误：{str(ctx['error'])[:120]}")

    # [5] Thinking
    lines.append("")
    th = r.get("thinking") or {}
    if (
        th.get("status") == "skipped"
        or th.get("verdict") is None
        and th.get("status") == "skipped"
    ):
        lines.append(f"[5/7] Thinking · {th.get('status', 'not_run')}")
    elif th.get("verdict") or th.get("disabled") or th.get("enabled"):
        lines.append("[5/7] Thinking")
        lines.append(f"  verdict：{th.get('verdict', 'unknown')}")
        d = th.get("disabled") or {}
        e = th.get("enabled") or {}
        if d:
            lines.append(
                f"  disable：{d.get('status')} http={d.get('http_status')} "
                f"answer={d.get('has_answer')} think_sig={d.get('has_thinking_signal')}"
            )
        if e:
            lines.append(
                f"  enable ：{e.get('status')} http={e.get('http_status')} "
                f"answer={e.get('has_answer')} think_sig={e.get('has_thinking_signal')}"
            )
    else:
        lines.append(f"[5/7] Thinking · {th.get('status', 'not_run')}")

    # [6] Tools
    lines.append("")
    tools = r.get("tools") or {}
    if tools.get("status") in ("pass", "fail", "error", "unsupported"):
        lines.append("[6/7] Tool use")
        icon = {"pass": "✅", "fail": "⚠", "error": "❌", "unsupported": "·"}.get(
            tools["status"], "·"
        )
        lines.append(
            f"  状态：{icon} {tools['status']} · support={tools.get('protocol_support')}"
        )
        if tools.get("tool_name_seen"):
            lines.append(f"  tool：{tools['tool_name_seen']}")
        if tools.get("error"):
            lines.append(f"  错误：{str(tools['error'])[:120]}")
    else:
        lines.append(f"[6/7] Tool use · {tools.get('status', 'not_run')}")

    # [7] Vision
    lines.append("")
    vis = r.get("vision") or {}
    if vis.get("status") in ("pass", "fail", "error", "unsupported"):
        lines.append("[7/7] Vision")
        icon = {"pass": "✅", "fail": "⚠", "error": "❌", "unsupported": "·"}.get(
            vis["status"], "·"
        )
        lines.append(f"  状态：{icon} {vis['status']}")
        if vis.get("answer"):
            lines.append(f'  答案："{vis["answer"]}"')
        if vis.get("error"):
            lines.append(f"  错误：{str(vis['error'])[:120]}")
    else:
        lines.append(f"[7/7] Vision · {vis.get('status', 'not_run')}")

    lines.append("")
    lines.append("-" * 60)
    lines.append(f"  总结：{r['summary']['verdict']}")
    lines.append("=" * 60)

    # 推荐操作
    rec = (r.get("summary") or {}).get("recommended_actions") or []
    if rec:
        lines.append("")
        lines.append("  💡 建议操作:")
        for action in rec:
            lines.append(f"    • {action}")

    return "\n".join(lines)


# ---------- cc-switch 运行日志（只读） ----------

LOGS_DIR = str(Path.home() / ".cc-switch" / "logs")


def _resolve_since_or_fail(args, output) -> tuple[int | None, int | None]:
    """解析日志命令的 --since；失败时输出提示并返回 (None, 2)。"""
    try:
        return parse_since(getattr(args, "since", None)), None
    except ValueError as exc:
        output(str(exc))
        return None, 2


def run_history(args, say) -> int:
    """history 子命令。"""
    since_ts, error_code = _resolve_since_or_fail(args, say)
    if error_code is not None:
        return error_code
    limit = getattr(args, "limit", 20) or 20
    fails = getattr(args, "fails", False)
    prov = getattr(args, "provider", None) or None
    rows = query_proxy_logs(
        args.db, since_ts=since_ts, limit=limit, fails_only=fails, provider_substr=prov
    )
    report = {
        "schema_version": 1,
        "command": "history",
        "since": getattr(args, "since", None),
        "limit": limit,
        "fails_only": fails,
        "count": len(rows),
        "entries": [
            {
                "created_at": r.get("created_at"),
                "created_at_fmt": r.get("created_at_fmt"),
                "provider_name": r.get("provider_name"),
                "app_type": r.get("app_type"),
                "request_model": r.get("request_model"),
                "model": r.get("model"),
                "status_code": r.get("status_code"),
                "latency_ms": r.get("latency_ms"),
                "first_token_ms": r.get("first_token_ms"),
                "input_tokens": r.get("input_tokens"),
                "output_tokens": r.get("output_tokens"),
                "total_cost_usd": r.get("total_cost_usd"),
                "error_message": r.get("error_message"),
                "error_category": r.get("error_category"),
                "routing_mismatch": r.get("routing_mismatch"),
                "data_source": r.get("data_source"),
            }
            for r in rows
        ],
    }
    # 可选磁盘日志
    log_file = getattr(args, "log_file", None)
    if log_file:
        report["log_file_tail"] = read_log_file_tail(
            log_file,
            lines=getattr(args, "log_lines", 50) or 50,
            keyword=getattr(args, "log_keyword", None),
        )

    if getattr(args, "json", False):
        print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    else:
        say(
            f"history: {len(rows)} 条"
            f"{'（仅失败）' if fails else ''}"
            f"{' since=' + str(args.since) if getattr(args, 'since', None) else ''}"
        )
        for i, r in enumerate(rows, 1):
            st = r.get("status_code")
            ok = st == 200 and not r.get("error_message")
            badge = _status_badge(ok, st, r.get("error_category"))
            pname = _sanitize_for_terminal(str(r.get("provider_name") or "?"))
            ts = r.get("created_at_fmt") or "?"
            _say_colored(f"[{i:02d}] {badge} {_c(ts, 'dim')}  {pname}")
            req_m = _sanitize_for_terminal(str(r.get("request_model") or "?"))
            act_m = _sanitize_for_terminal(str(r.get("model") or "?"))
            model_line = f"     {req_m} -> {act_m}"
            if r.get("routing_mismatch"):
                model_line += _c("  ⚡路由不一致", "yellow")
            _say_colored(
                f"{model_line}  "
                f"status={st}  lat={r.get('latency_ms')}ms  ttft={r.get('first_token_ms')}ms"
                f"  in={r.get('input_tokens') or 0} out={r.get('output_tokens') or 0}"
                f"  cost=${float(r.get('total_cost_usd') or 0):.4f}"
            )
            if r.get("error_message"):
                cat = _sanitize_for_terminal(str(r.get("error_category") or ""))
                msg = _sanitize_for_terminal(str(r.get("error_message") or ""))[:160]
                _say_colored(f"     {_c('[' + cat + ']', 'red')} {msg}")
        if log_file and report.get("log_file_tail"):
            say(f"\n--- log file tail: {log_file} ---")
            for ln in report["log_file_tail"]:
                say(ln)
    return 0


def run_stats(args, say) -> int:
    since_ts, error_code = _resolve_since_or_fail(args, say)
    if error_code is not None:
        return error_code
    stats = query_stats(args.db, since_ts=since_ts)
    include_deleted = getattr(args, "include_deleted", False)
    if not include_deleted:
        stats = [s for s in stats if not s.get("is_deleted")]
    report = {
        "schema_version": 1,
        "command": "stats",
        "since": getattr(args, "since", None),
        "include_deleted": include_deleted,
        "providers": stats,
    }
    if getattr(args, "json", False):
        print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    else:
        say(
            f"stats: {len(stats)} 个供应商"
            f"{' since=' + str(args.since) if getattr(args, 'since', None) else ''}"
            f"{'（已隐藏已删除供应商，--include-deleted 显示）' if not include_deleted else ''}"
        )
        hdr = (
            f"{_pad('供应商', 24)} {'请求':>6} {'成功%':>7} {'失败':>5} "
            f"{_pad('主失败因', 22)} {'中位延迟':>9} {'路由≠%':>7} {'成本$':>8} {'入token':>9} {'出token':>9}"
        )
        _say_colored(_c(hdr, "bold"))
        say("-" * 120)
        for s in stats:
            sr = s["success_rate"]
            rate_s = f"{sr * 100:.0f}%"
            if sr >= 0.95:
                rate_c = _c(rate_s, "green")
            elif sr >= 0.7:
                rate_c = _c(rate_s, "yellow")
            else:
                rate_c = _c(rate_s, "red", "bold")
            med_val = s["median_latency_ms"]
            med = f"{med_val:.0f}ms" if med_val is not None else "-"
            # 延迟 >10s 标红，>3s 标黄
            if med_val is not None and med_val > 10000:
                med_c = _c(med, "red", "bold")
            elif med_val is not None and med_val > 3000:
                med_c = _c(med, "yellow")
            else:
                med_c = med
            mm_r = s["mismatch_rate"]
            mm_s = f"{mm_r * 100:.0f}%"
            # 路由不一致 >50% 标红，>10% 标黄
            if mm_r > 0.5:
                mm_c = _c(mm_s, "red", "bold")
            elif mm_r > 0.1:
                mm_c = _c(mm_s, "yellow")
            else:
                mm_c = mm_s
            cat = s.get("top_fail_category") or "-"
            cat_c = _c(_pad(cat[:22], 22), "red") if cat != "-" else _pad(cat[:22], 22)
            fail_c = _c(str(s["fail"]), "red") if s["fail"] > 0 else str(s["fail"])
            cost = s.get("cost_usd") or 0.0
            cost_s = f"{cost:.2f}" if cost > 0 else "-"
            inp_tok = s.get("input_tokens") or 0
            out_tok = s.get("output_tokens") or 0
            inp_s = f"{inp_tok:,}" if inp_tok else "-"
            out_s = f"{out_tok:,}" if out_tok else "-"
            pname = _sanitize_for_terminal(s["provider_name"][:24])
            _say_colored(
                f"{_pad(pname, 24)} {s['total']:6d} {rate_c:>7} {fail_c:>5} "
                f"{cat_c} {med_c:>9} {mm_c:>7} {cost_s:>8} {inp_s:>9} {out_s:>9}"
            )
    return 0


def run_routing(args, say) -> int:
    since_ts, error_code = _resolve_since_or_fail(args, say)
    if error_code is not None:
        return error_code
    limit = getattr(args, "limit", 20) or 20
    pairs = query_routing(args.db, since_ts=since_ts, limit=limit)
    report = {
        "schema_version": 1,
        "command": "routing",
        "since": getattr(args, "since", None),
        "pairs": pairs,
    }
    if getattr(args, "json", False):
        print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    else:
        say(
            f"routing: top {len(pairs)} 静默路由"
            f"{' since=' + str(args.since) if getattr(args, 'since', None) else ''}"
        )
        for i, p in enumerate(pairs, 1):
            say(
                f"[{i:02d}] {p['count']:5d}×  {p['request_model']}  =>  {p['actual_model']}"
            )
    return 0


def run_health(args, say) -> int:
    """health 子命令：读 cc-switch 的 provider_health（被动流量健康度）。

    注意：这是 cc-switch 从真实代理流量聚合的健康判定（如额度用完/502/529），
    不是主动可用性探测，也不校验答案正确性。主动探测请用 check / inspect。
    """
    rows = query_provider_health(args.db)
    report = {
        "schema_version": 1,
        "command": "health",
        "source": "cc-switch provider_health（被动流量聚合，非主动探测）",
        "total": len(rows),
        "unhealthy": sum(1 for r in rows if not r.get("is_healthy")),
        "providers": rows,
    }
    if getattr(args, "json", False):
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str), flush=True)
        return 0
    if not rows:
        say("provider_health 表无数据（cc-switch 未记录流量健康度）")
        return 0
    say(
        f"health: {len(rows)} 个供应商"
        f"（cc-switch 被动流量健康度，非主动探测；主动探测请用 check）"
    )
    hdr = f"{_pad('供应商', 24)} {'健康':>4} {'连续失败':>8} {'最近错误':40}"
    _say_colored(_c(hdr, "bold"))
    say("-" * 90)
    for r in rows:
        healthy = r.get("is_healthy")
        name = _sanitize_for_terminal(str(r.get("provider_name") or "?")[:24])
        h_c = _c("✓", "green") if healthy else _c("✗", "red", "bold")
        fails = r.get("consecutive_failures") or 0
        fail_c = _c(str(fails), "red", "bold") if fails > 0 else "0"
        err = (r.get("last_error") or "-")[:60]
        err = _sanitize_for_terminal(err)
        _say_colored(f"{_pad(name, 24)} {h_c:>4} {fail_c:>8} {err}")
    return 0


def _filter_new_watch_rows(rows, last_ts, seen_ids) -> list[dict]:
    """筛出新 watch 行：created_at >= last_ts 且 (request_id, created_at) 组合键未见过。"""
    out = []
    for r in rows:
        rid = str(r.get("request_id") or "")
        cts = r.get("created_at") or 0
        key = f"{rid}|{cts}"
        if cts >= last_ts and key not in seen_ids:
            out.append(r)
    return out


def run_watch(args, say) -> int:
    """轮询 proxy_request_logs，有新行就打印（Ctrl+C 退出）。"""
    interval = max(1, int(getattr(args, "interval", 3) or 3))
    fails_only = getattr(args, "fails", False)
    prov = getattr(args, "provider", None) or None
    # 起点：当前最新 created_at（避免启动时刷屏历史）
    bootstrap = query_proxy_logs(
        args.db, limit=1, fails_only=False, provider_substr=prov
    )
    last_ts = bootstrap[0]["created_at"] if bootstrap else int(time.time())
    seen_ids: set[str] = set()
    if bootstrap:
        rid = str(bootstrap[0].get("request_id") or "")
        cts = bootstrap[0].get("created_at") or 0
        if rid:
            seen_ids.add(f"{rid}|{cts}")
    say(
        f"watch: 每 {interval}s 轮询 proxy_request_logs"
        f"{'（仅失败）' if fails_only else ''}"
        f"{' provider~' + prov if prov else ''}"
    )
    say(f"从 created_at>{last_ts} 开始；Ctrl+C 结束\n")
    try:
        while True:
            # 多取一些，按 id 去重
            rows = query_proxy_logs(
                args.db,
                since_ts=int(last_ts) if last_ts else None,
                limit=50,
                fails_only=fails_only,
                provider_substr=prov,
            )
            # query 是 DESC；反转让旧的先打
            new_rows = _filter_new_watch_rows(list(reversed(rows)), last_ts, seen_ids)
            for r in new_rows:
                rid = str(r.get("request_id") or "")
                cts = r.get("created_at") or 0
                seen_ids.add(f"{rid}|{cts}")
                st = r.get("status_code")
                ok = st == 200 and not r.get("error_message")
                badge = _status_badge(ok, st, r.get("error_category"))
                pname = _sanitize_for_terminal(str(r.get("provider_name") or "?"))
                ts = r.get("created_at_fmt") or "?"
                _say_colored(f"{badge} {_c(ts, 'dim')}  {pname}")
                req_m = _sanitize_for_terminal(str(r.get("request_model") or "?"))
                act_m = _sanitize_for_terminal(str(r.get("model") or "?"))
                model_line = f"  {req_m} -> {act_m}"
                if r.get("routing_mismatch"):
                    model_line += _c("  ⚡路由", "yellow")
                _say_colored(
                    f"{model_line}  "
                    f"status={st}  lat={r.get('latency_ms')}ms  ttft={r.get('first_token_ms')}ms"
                )
                if r.get("error_message"):
                    cat = _sanitize_for_terminal(str(r.get("error_category") or ""))
                    msg = _sanitize_for_terminal(str(r.get("error_message") or ""))[
                        :160
                    ]
                    _say_colored(f"  {_c('[' + cat + ']', 'red')} {msg}")
                if r.get("created_at") and r["created_at"] > last_ts:
                    last_ts = r["created_at"]
            # 防止 seen 无限涨
            if len(seen_ids) > 5000:
                seen_ids = set(list(seen_ids)[-2000:])
            time.sleep(interval)
    except KeyboardInterrupt:
        say("\nwatch 已停止")
        return 0


# ---------- analyze：多维聚合分析（只读，纯内存计算） ----------


def _color_rate(rate: float) -> str:
    """成功率带色：≥0.95 绿 / ≥0.80 黄 / <0.80 红。"""
    s = f"{rate * 100:.0f}%"
    if rate >= 0.95:
        return _c(s, "green")
    if rate >= 0.80:
        return _c(s, "yellow")
    return _c(s, "red", "bold")


def _fmt_ms(v) -> str:
    return f"{v:.0f}ms" if isinstance(v, (int, float)) else "-"


def run_analyze(args, say) -> int:
    """analyze 子命令：默认全维度报表，可用 --mode 选单个维度。"""
    since_ts, error_code = _resolve_since_or_fail(args, say)
    if error_code is not None:
        return error_code
    id_map = load_provider_id_map(args.db)
    rows = query_analyze_raw(args.db, since_ts=since_ts)
    mode = getattr(args, "mode", "all") or "all"
    prov = getattr(args, "provider", None) or None

    report: dict = {
        "schema_version": 1,
        "command": "analyze",
        "since": getattr(args, "since", None),
        "mode": mode,
        "provider": prov,
        "row_count": len(rows),
    }
    if mode in ("all", "provider-day"):
        report["by_provider_day"] = analyze_by_provider_day(rows, id_map)
    if mode in ("all", "model"):
        report["by_model"] = analyze_by_model(rows)
    if mode in ("all", "day"):
        report["by_day"] = analyze_by_day(rows)
    if prov and mode in ("all", "provider"):
        report["provider_deep"] = analyze_provider_deep(rows, prov, id_map)

    if getattr(args, "json", False):
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str), flush=True)
        return 0

    say(
        f"analyze: {len(rows)} 条记录"
        f"{' since=' + str(args.since) if getattr(args, 'since', None) else ''}"
        f"  mode={mode}"
        f"{'  provider~' + prov if prov else ''}"
    )
    if not rows:
        say("（该时间窗内无记录）")
        return 0

    # ---- by_day ----
    if "by_day" in report:
        by_day = report["by_day"]
        _say_colored(
            _c(
                "\n[按天] 日期 · 请求 · 成功率 · 独立供应商 · p50/p95 延迟 · 主失败因",
                "bold",
            )
        )
        say("-" * 90)
        rates = [d["success_rate"] for d in by_day]
        p50s = [d["lat_p50"] or 0 for d in by_day]
        for d in by_day:
            rate_c = _color_rate(d["success_rate"])
            cat = d.get("top_fail_category") or "-"
            cat_c = _c(cat[:20], "red") if cat != "-" else cat[:20]
            _say_colored(
                f"  {d['date']}  {d['total']:5d}  {rate_c:>7}  "
                f"prov={d['unique_providers']:2d}  "
                f"p50={_fmt_ms(d['lat_p50']):>7}  p95={_fmt_ms(d['lat_p95']):>7}  {cat_c}"
            )
        if len(by_day) >= 2:
            spark_rate = _sparkline(rates, width=min(30, len(rates)))
            spark_lat = _sparkline(p50s, width=min(30, len(p50s)))
            say(f"  成功率趋势 {spark_rate}   p50 延迟 {spark_lat}")

    # ---- by_model ----
    if "by_model" in report:
        by_model = report["by_model"][:20]
        _say_colored(
            _c(
                "\n[按模型] 模型 · 请求 · 成功率 · p50/p95/p99 延迟 · 平均 tokens(in/out)",
                "bold",
            )
        )
        say("-" * 100)
        for m in by_model:
            rate_c = _color_rate(m["success_rate"])
            mname = _sanitize_for_terminal(str(m["model"])[:36])
            avg_in = m.get("avg_input_tokens")
            avg_out = m.get("avg_output_tokens")
            tok = (
                f"{avg_in:.0f}/{avg_out:.0f}"
                if avg_in is not None and avg_out is not None
                else "-"
            )
            _say_colored(
                f"  {mname:36} {m['total']:6d}  {rate_c:>7}  "
                f"p50={_fmt_ms(m['lat_p50']):>7} p95={_fmt_ms(m['lat_p95']):>7} p99={_fmt_ms(m['lat_p99']):>7}  "
                f"tok={tok}"
            )

    # ---- by_provider_day ----
    if "by_provider_day" in report:
        bpd = report["by_provider_day"]
        _say_colored(
            _c("\n[供应商 × 日期] 每格显示成功率；(总/失败) 汇总在最右列", "bold")
        )
        say("-" * 100)
        days = bpd["days"]
        if len(days) > 14:
            days_show = days[-14:]
        else:
            days_show = days
        head_days = " ".join(f"{d[-5:]:>7}" for d in days_show)  # MM-DD
        _say_colored(_c(f"  {'供应商':24} {head_days}   {'总计':>10}", "dim"))
        for pcell in bpd["providers"]:
            pname = _sanitize_for_terminal(pcell["provider_name"][:24])
            cells_str = []
            for d in days_show:
                idx = days.index(d)
                c = pcell["days"][idx]
                if c is None:
                    cells_str.append(f"{_c('  -  ', 'dim'):>7}")
                else:
                    cells_str.append(f"{_color_rate(c['success_rate']):>7}")
            rt = pcell["row_totals"]
            tot_c = _color_rate(rt["success_rate"])
            _say_colored(
                f"  {pname:24} {' '.join(cells_str)}   {rt['total']:5d}/{rt['fail']:<3d} {tot_c}"
            )
        if len(days) > len(days_show):
            say(
                f"  （仅显示最近 {len(days_show)} 天；全量共 {len(days)} 天，见 --json）"
            )

    # ---- provider_deep ----
    if report.get("provider_deep"):
        pd = report["provider_deep"]
        _say_colored(
            _c(
                f"\n[供应商深度] provider~'{prov}'  匹配={len(pd['match_provider_ids'])}"
                f"  总请求={pd['total']}",
                "bold",
            )
        )
        if pd["match_provider_names"]:
            say(f"  命中: {', '.join(pd['match_provider_names'])}")
        say("-" * 90)
        if pd["by_model"]:
            _say_colored(_c("  · 按模型", "dim"))
            for m in pd["by_model"][:10]:
                rate_c = _color_rate(m["success_rate"])
                mname = _sanitize_for_terminal(str(m["model"])[:36])
                _say_colored(
                    f"    {mname:36} {m['total']:5d}  {rate_c:>7}  "
                    f"p50={_fmt_ms(m['lat_p50']):>7} p95={_fmt_ms(m['lat_p95']):>7}"
                )
        if pd["by_day"]:
            _say_colored(_c("  · 按天", "dim"))
            for d in pd["by_day"]:
                _say_colored(
                    f"    {d['date']}  {d['total']:5d}  {_color_rate(d['success_rate']):>7}  "
                    f"p50={_fmt_ms(d['lat_p50']):>7}"
                )

    return 0


def format_history_sidebar(db_path: str, provider_name: str, since: str = "24h") -> str:
    """给 check/inspect 附带的一行历史摘要。"""
    try:
        since_ts = parse_since(since)
    except ValueError:
        since_ts = parse_since("24h")
    s = summarize_provider_history(db_path, provider_name, since_ts=since_ts)
    if not s:
        return f"  history({since}): 无记录"
    rate = f"{s['success_rate'] * 100:.0f}%"
    med = (
        f"{s['median_latency_ms']:.0f}ms" if s["median_latency_ms"] is not None else "-"
    )
    mm = f"{s['mismatch_rate'] * 100:.0f}%"
    cat = s.get("top_fail_category") or "-"
    return (
        f"  history({since}): 请求{s['total']} 成功{rate} 失败{s['fail']}"
        f" 主因={cat} 中位延迟={med} 路由≠{mm}"
    )


SUBCOMMANDS = (
    "check",
    "list-models",
    "inspect",
    "history",
    "stats",
    "routing",
    "watch",
    "analyze",
    "env-check",
    "trend",
    "health",
)
# 主解析器上带值的全局选项：扫描子命令时要连它的值一起跳过，
# 否则 `--db list-models` 这类会把选项的值误认成子命令。
_GLOBAL_VALUE_OPTS = (
    "--db",
    "--timeout",
    "--workers",
    "--user-agent",
    "--stainless-version",
)


def _inject_default_command(argv: list[str]) -> list[str]:
    """无子命令时注入 check，兼容文档中的「可省略 check」用法。

    只看 argv[1] 不够：全局选项可以写在子命令前面，
    `--timeout 5 list-models` 的 argv[1] 是选项，但子命令确实存在。
    因此要跳过前导的全局选项（含其值）再判断。

    例：
      prog --failover-only            →  prog check --failover-only
      prog --timeout 5 list-models    →  原样返回（子命令已存在）
      prog                            →  prog check
    """
    if len(argv) <= 1:
        return argv + ["check"]
    i = 1
    while i < len(argv):
        tok = argv[i]
        if tok in SUBCOMMANDS or tok in ("-h", "--help"):
            return argv  # 子命令已存在，不注入
        if tok in _GLOBAL_VALUE_OPTS:
            i += 2  # 跳过选项及其值
            continue
        if tok.startswith("-"):
            i += 1  # 开关型选项或 --opt=value
            continue
        return argv  # 位置参数但不是已知子命令，交给 argparse 报错
    # 全程只有全局选项，没有子命令 → 注入 check
    return [argv[0], "check"] + argv[1:]


def _make_common(suppress_defaults: bool):
    """公共选项。子解析器版用 SUPPRESS 作 default，避免覆盖主解析器已解析的前置值。

    全局选项可写在子命令前（`--timeout 5 check`）或后（`check --timeout 5`）。
    两处都带 default 时，子解析器会用自己的 default 盖掉前置传入的值，
    造成 `--timeout 5 check` 静默失效；子解析器改用 SUPPRESS 后只在显式传参时赋值。
    """

    def dflt(v):
        return argparse.SUPPRESS if suppress_defaults else v

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--db", default=dflt(DB_PATH), help=f"cc-switch.db 路径 (默认: {DB_PATH})"
    )
    common.add_argument(
        "--skip-tls-verify",
        action="store_true",
        default=dflt(False),
        help="危险：跳过 TLS 证书验证，仅用于信任的自签名中转站",
    )
    common.add_argument(
        "--timeout", type=int, default=dflt(30), help="单请求超时秒 (默认: 30)"
    )
    common.add_argument("--workers", type=int, default=dflt(6), help="并发数 (默认: 6)")
    common.add_argument(
        "--user-agent",
        default=dflt(None),
        help="覆盖 User-Agent（默认用本机 claude --version 探测的版本）",
    )
    # 注意：--probe-max-tokens / --probe-enable-thinking 故意不放进 common。
    # common 会被主解析器继承，若把这两个加进去，主解析器会跟 list-models 的
    # --probe 撞前缀歧义（--probe could match --probe-max-tokens, --probe-enable-thinking）。
    # 这两个选项只对 check/inspect/list-models 有意义，挂在各子解析器上即可。
    common.add_argument(
        "--stainless-version",
        default=dflt(None),
        help="覆盖 x-stainless-package-version 指纹头（无法从 claude --version 推导 SDK 版本）",
    )
    return common


def _build_parser():
    """构造 argparse，公共选项 + 三个子命令：check / list-models / inspect。"""
    common = _make_common(suppress_defaults=True)  # 子解析器共用
    root_common = _make_common(suppress_defaults=False)  # 主解析器，提供真实默认值

    ap = argparse.ArgumentParser(
        description="CC-Pulse：cc-switch 供应商健康检测与单模型深度诊断",
        parents=[root_common],
    )
    sub = ap.add_subparsers(dest="command")

    # check：日常健康检测（默认子命令，也可显式写 check）
    p_check = sub.add_parser(
        "check", parents=[common], help="对供应商进行真实问题探测（默认行为）"
    )
    p_check.add_argument(
        "--type",
        default="claude",
        choices=["claude", "codex", "openclaw", "all"],
        help="检测哪类供应商 (默认: claude)",
    )
    p_check.add_argument(
        "--failover-only",
        action="store_true",
        help="只测故障转移队列里的供应商（含当前激活的）",
    )
    p_check.add_argument(
        "--current-only",
        action="store_true",
        help="只测当前激活的供应商（最窄；与 --failover-only 同时设时本项优先）",
    )
    p_check.add_argument(
        "--provider",
        default=None,
        help="按供应商名过滤（支持单个名、逗号分隔多个名或子串）",
    )
    p_check.add_argument(
        "--select",
        action="store_true",
        help="交互式选择供应商（↑↓ 移动、空格多选、回车确认；非 TTY 自动忽略）",
    )
    p_check.add_argument(
        "--stealth",
        action="store_true",
        help=f"隐身模式：并发降至≤{STEALTH_MAX_WORKERS} 且每档请求前随机延迟，弱化脚本式流量尖峰（较慢；仅 check）",
    )
    p_check.add_argument(
        "--json",
        action="store_true",
        help="输出结构化 JSON 报告到 stdout（人类可读文本保留到 stderr）",
    )
    p_check.add_argument(
        "--with-history",
        action="store_true",
        help="每个供应商探测结果后附加 cc-switch 近 24h 日志摘要",
    )
    p_check.add_argument(
        "--history-since",
        default="24h",
        help="--with-history 时间窗口（默认 24h；如 7d / 30m）",
    )
    p_check.add_argument(
        "--archive",
        default=None,
        help="探测历史归档路径（默认 ~/.cc-pulse/probe_history.jsonl）",
    )
    p_check.add_argument(
        "--retry",
        type=int,
        default=0,
        help="网络层错误重试次数（默认 0 不重试，仅对 URLError/TimeoutError/OSError 生效）",
    )
    # 探测 token 预算：故意挂在 check 子解析器，不放 common（见 common 定义处说明）
    p_check.add_argument(
        "--probe-max-tokens",
        type=int,
        default=PROBE_MAX_TOKENS,
        help=f"探测请求 max_tokens 预算（默认 {PROBE_MAX_TOKENS}，自然值；这是上限非实际消耗）",
    )
    p_check.add_argument(
        "--probe-enable-thinking",
        action="store_true",
        help="允许探测请求走 thinking 模式（默认禁用，避免 DeepSeek 等 thinking 模型耗光 max_tokens）",
    )
    p_check.add_argument(
        "--alert-threshold",
        type=float,
        default=None,
        help="可用率告警阈值（0-1，如 0.8 表示低于 80% 告警）；cron 巡检可据此触发",
    )

    # list-models：拉取供应商 /v1/models
    p_lm = sub.add_parser(
        "list-models",
        parents=[common],
        help="拉取每个供应商实际支持的模型列表（GET /v1/models）",
    )
    p_lm.add_argument(
        "--type",
        default="claude",
        choices=["claude", "codex", "openclaw", "all"],
        help="检测哪类供应商 (默认: claude)",
    )
    p_lm.add_argument(
        "--failover-only",
        action="store_true",
        help="只测故障转移队列里的供应商（含当前激活的）",
    )
    p_lm.add_argument(
        "--current-only",
        action="store_true",
        help="只测当前激活的供应商（最窄；与 --failover-only 同时设时本项优先）",
    )
    p_lm.add_argument(
        "--provider",
        default=None,
        help="按供应商名过滤（支持单个名、逗号分隔多个名或子串）",
    )
    p_lm.add_argument(
        "--select",
        action="store_true",
        help="交互式选择供应商（↑↓ 移动、空格多选、回车确认；非 TTY 自动忽略）",
    )
    p_lm.add_argument(
        "--probe",
        action="store_true",
        help="对每个模型发轻量探测（2+3 算术题），验证是否真能用",
    )
    p_lm.add_argument(
        "--deep",
        action="store_true",
        help="深度探测每个模型（text/streaming/metadata/thinking/tools 五维度；较慢）",
    )
    p_lm.add_argument(
        "--source",
        default="listed",
        choices=["configured", "listed", "both"],
        help="探测哪些模型：configured=配置档位 / listed=GET /v1/models / both=合并（默认 listed）",
    )
    p_lm.add_argument(
        "--json",
        action="store_true",
        help="以 JSON 输出模型目录（含 --probe/--deep 探测结果）",
    )
    p_lm.add_argument(
        "--probe-max-tokens",
        type=int,
        default=PROBE_MAX_TOKENS,
        help=f"探测请求 max_tokens 预算（默认 {PROBE_MAX_TOKENS}，自然值；这是上限非实际消耗）",
    )
    p_lm.add_argument(
        "--probe-enable-thinking",
        action="store_true",
        help="允许探测请求走 thinking 模式（默认禁用，避免 DeepSeek 等 thinking 模型耗光 max_tokens）",
    )

    # inspect：单一模型深度检测
    p_inspect = sub.add_parser(
        "inspect", parents=[common], help="对单一 (provider, model) 三元组进行深度诊断"
    )
    p_inspect.add_argument(
        "--provider",
        default=None,
        help="供应商名称（与 cc-switch 中一致）；--compare 时可选",
    )
    p_inspect.add_argument(
        "--select",
        action="store_true",
        help="交互式选择供应商（↑↓ 移动、空格多选、回车确认；非 TTY 自动忽略）",
    )
    p_inspect.add_argument(
        "--model",
        default=None,
        help="模型 ID（精确匹配，可包含 [1M] 等后缀）；--all-models/--models 时忽略",
    )
    p_inspect.add_argument(
        "--all-models",
        action="store_true",
        help="批量检测该供应商的多个模型（配合 --source 决定范围）",
    )
    p_inspect.add_argument(
        "--models",
        default=None,
        help="逗号分隔的模型 ID 列表（自定义组合，如 glm-5-2,deepseek-v4-pro）",
    )
    p_inspect.add_argument(
        "--source",
        default="configured",
        choices=["configured", "listed", "manual"],
        help="模型来源：configured(cc-switch 配置)、"
        "listed(供应商 /v1/models 声明)、manual(强制) (默认: configured)",
    )
    p_inspect.add_argument(
        "--type",
        default="claude",
        choices=["claude", "codex", "openclaw", "all"],
        help="限定供应商类型 (默认: claude)",
    )
    p_inspect.add_argument(
        "--keep-suffix",
        action="store_true",
        help="保留模型 ID 中的 [1M] 等后缀（默认会去后缀）",
    )
    p_inspect.add_argument(
        "--include",
        default=None,
        help="要执行的检查项，逗号分隔；支持：text,streaming,"
        "model-consistency,protocol,error-classification,metadata,thinking,tools。"
        "默认全开（不含 vision）；--compare 默认仅 text,streaming",
    )
    p_inspect.add_argument(
        "--ttft-timeout",
        type=int,
        default=None,
        help="流式探测首 token 超时（秒），默认使用 --timeout",
    )
    p_inspect.add_argument(
        "--with-metadata",
        action="store_true",
        help=argparse.SUPPRESS,  # 已废弃，metadata 默认包含在 --include 中
    )
    p_inspect.add_argument(
        "--probe-context",
        choices=["512k", "1m"],
        default="512k",
        help="上下文窗口探测档位：512k（默认）或 1m；仅在元数据无声明时触发",
    )
    p_inspect.add_argument(
        "--human",
        action="store_true",
        help="以人类可读格式输出到 stdout（默认 JSON；与 --format human 等价）",
    )
    p_inspect.add_argument(
        "--format",
        default=None,
        choices=["human", "json"],
        dest="output_format",
        help="输出格式：human / json（默认 json；--human 等价于 human）",
    )
    p_inspect.add_argument(
        "--compare",
        default=None,
        help="对比模式：逗号分隔的 'provider/model' 列表，"
        "如 'Relay-A/claude-sonnet-4-6,Relay-B/glm-5'。"
        "同一道题逐个打多个目标，输出对齐的对比报告；"
        "此模式不需要 --provider",
    )
    p_inspect.add_argument(
        "--quiet",
        action="store_true",
        help="静默模式：只输出 NDJSON（每模型一行 JSON 到 stdout），关闭所有进度提示；"
        "与 --human 互斥。退出码：0 全成功 / 3 部分失败 / 4 全部失败",
    )
    p_inspect.add_argument(
        "--probe-delay",
        type=float,
        default=3.0,
        help="批量模式模型间延迟秒（默认 3.0，防 429）",
    )
    p_inspect.add_argument(
        "--max-retries",
        type=int,
        default=1,
        help="rate_limit(429) 时重试次数（默认 1）",
    )
    p_inspect.add_argument(
        "--with-history", action="store_true", help="报告中附加该供应商近 24h 日志摘要"
    )
    p_inspect.add_argument(
        "--history-since", default="24h", help="--with-history 时间窗口（默认 24h）"
    )
    p_inspect.add_argument(
        "--archive",
        default=None,
        help="探测历史归档路径（默认 ~/.cc-pulse/probe_history.jsonl）",
    )
    p_inspect.add_argument(
        "--retry",
        type=int,
        default=0,
        help="网络层错误重试次数（默认 0 不重试，仅对 URLError/TimeoutError/OSError 生效）",
    )
    p_inspect.add_argument(
        "--probe-max-tokens",
        type=int,
        default=PROBE_MAX_TOKENS,
        help=f"探测请求 max_tokens 预算（默认 {PROBE_MAX_TOKENS}，自然值；这是上限非实际消耗）",
    )
    p_inspect.add_argument(
        "--probe-enable-thinking",
        action="store_true",
        help="允许探测请求走 thinking 模式（默认禁用，避免 DeepSeek 等 thinking 模型耗光 max_tokens）",
    )

    # history / stats / routing：只读日志，不发 HTTP
    p_hist = sub.add_parser(
        "history", parents=[common], help="读取 cc-switch 代理请求日志（最近 N 条）"
    )
    p_hist.add_argument("--limit", type=int, default=20, help="条数（默认 20）")
    p_hist.add_argument("--fails", action="store_true", help="只显示失败记录")
    p_hist.add_argument("--since", default=None, help="时间窗口：24h / 7d / 30m / 秒数")
    p_hist.add_argument("--provider", default=None, help="按供应商名子串过滤")
    p_hist.add_argument("--json", action="store_true", help="JSON 输出")
    p_hist.add_argument(
        "--log-file",
        default=None,
        help="可选：额外打印磁盘日志尾部（如 ~/.cc-switch/logs/cc-switch.log）",
    )
    p_hist.add_argument(
        "--log-lines", type=int, default=50, help="磁盘日志尾部行数（默认 50）"
    )
    p_hist.add_argument("--log-keyword", default=None, help="磁盘日志关键词过滤")

    p_stats = sub.add_parser(
        "stats", parents=[common], help="按供应商汇总成功率/延迟/路由不一致/成本"
    )
    p_stats.add_argument("--since", default="7d", help="时间窗口（默认 7d）")
    p_stats.add_argument(
        "--include-deleted", action="store_true", help="包含已从 cc-switch 删除的供应商"
    )
    p_stats.add_argument("--json", action="store_true", help="JSON 输出")

    p_route = sub.add_parser(
        "routing",
        parents=[common],
        help="静默路由排行（request_model => actual model）",
    )
    p_route.add_argument("--since", default="7d", help="时间窗口（默认 7d）")
    p_route.add_argument("--limit", type=int, default=20, help="显示条数（默认 20）")
    p_route.add_argument("--json", action="store_true", help="JSON 输出")

    p_watch = sub.add_parser(
        "watch",
        parents=[common],
        help="实时轮询 proxy_request_logs，有新记录就打印（Ctrl+C 结束）",
    )
    p_watch.add_argument("--interval", type=int, default=3, help="轮询间隔秒（默认 3）")
    p_watch.add_argument("--fails", action="store_true", help="只显示失败")
    p_watch.add_argument("--provider", default=None, help="按供应商名子串过滤")

    p_analyze = sub.add_parser(
        "analyze", parents=[common], help="多维度聚合分析（按天/模型/供应商交叉）"
    )
    p_analyze.add_argument("--since", default="7d", help="时间窗口（默认 7d）")
    p_analyze.add_argument(
        "--mode",
        default="all",
        choices=["all", "day", "model", "provider-day", "provider"],
        help="分析维度（默认 all 全部）",
    )
    p_analyze.add_argument(
        "--provider", default=None, help="单供应商深度（--mode provider 或 all 时生效）"
    )
    p_analyze.add_argument("--json", action="store_true", help="JSON 输出")

    p_env = sub.add_parser(
        "env-check",
        parents=[common],
        help="检测环境变量是否覆盖 cc-switch 所选供应商（静默路由排查）",
    )
    p_env.add_argument("--json", action="store_true", help="JSON 输出")

    p_trend = sub.add_parser(
        "trend",
        parents=[common],
        help="读取本地探测归档，按供应商/模型聚合趋势（成功率/延迟分位/错误分类）",
    )
    p_trend.add_argument(
        "--since", default="7d", help="时间窗口：24h / 7d / 30m / 秒数（默认 7d）"
    )
    p_trend.add_argument(
        "--archive",
        default=None,
        help="归档文件路径（默认 ~/.cc-pulse/probe_history.jsonl）",
    )
    p_trend.add_argument("--provider", default=None, help="只统计指定供应商")
    p_trend.add_argument("--model", default=None, help="只统计指定模型")
    p_trend.add_argument(
        "--include-test",
        action="store_true",
        help="包含测试数据供应商（Mock-Provider/Prov-A 等，默认排除）",
    )
    p_trend.add_argument("--json", action="store_true", help="JSON 输出")

    p_health = sub.add_parser(
        "health",
        parents=[common],
        help="读 cc-switch 被动流量健康度（provider_health，非主动探测）",
    )
    p_health.add_argument("--json", action="store_true", help="JSON 输出")

    p_deep = sub.add_parser(
        "deep-dive",
        parents=[common],
        help="从 check JSON 批量深挖失败/可用供应商（下沉自 PS1，CI 可串联）",
    )
    p_deep.add_argument(
        "--from",
        dest="from_file",
        required=True,
        help="check JSON 文件路径，或 - 读 stdin",
    )
    p_deep.add_argument(
        "--target",
        default="fail",
        choices=["fail", "ok", "both"],
        help="深挖目标：fail/ok/both（默认 fail）",
    )
    p_deep.add_argument(
        "--models", default=None, help="指定模型（逗号分隔，默认全部去重）"
    )
    p_deep.add_argument(
        "--yes", action="store_true", help="跳过容量确认（>20 组合时）"
    )
    p_deep.add_argument("--json", action="store_true", help="只输出任务列表 JSON 不执行")

    # 兜底默认（注入 check 后子解析器会覆盖这些）
    ap.set_defaults(command="check", type="claude", failover_only=False, json=False)
    return ap, common, p_check, p_lm, p_inspect, p_hist, p_stats, p_route, p_watch


def run_deep_dive(args, say) -> int:
    """从 check JSON 读结果，对失败/可用供应商批量深挖（逐个调 inspect）。

    下沉自 PS1 的 Ask-DeepDive/Invoke-DeepDive，让 CI 可串联
    `ccpulse check --json | ccpulse deep-dive --from - --target fail`。
    """
    src = args.from_file
    try:
        if src == "-":
            data = json.load(sys.stdin)
        else:
            with open(src, encoding="utf-8") as f:
                data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        say(f"读取 check JSON 失败: {e}")
        return 2
    providers = data.get("providers") or []
    if not providers:
        say("check JSON 无 providers")
        return 2

    # 过滤目标供应商
    target = (args.target or "fail").lower()
    if target == "fail":
        targets = [p for p in providers if not p.get("overall_ok")]
    elif target == "ok":
        targets = [p for p in providers if p.get("overall_ok")]
    elif target == "both":
        targets = list(providers)
    else:
        say(f"未知 --target: {target}（fail/ok/both）")
        return 2
    if not targets:
        say(f"无符合的供应商（target={target}）")
        return 0

    # 去重模型（attempts[].model）
    seen: dict[str, bool] = {}
    all_models: list[str] = []
    for p in targets:
        for a in (p.get("attempts") or []):
            m = a.get("model")
            if m and m not in seen:
                seen[m] = True
                all_models.append(m)
    if args.models:
        sel = {m.strip() for m in args.models.split(",") if m.strip()}
        models = [m for m in all_models if m in sel] or list(sel)
    else:
        models = all_models
    if not models:
        say("无候选模型")
        return 0

    # 任务列表：供应商 × 模型
    tasks = [(p["name"], p.get("type", "claude"), m) for p in targets for m in models]

    # 容量保护
    if len(tasks) > 20 and not args.yes:
        est_min = len(tasks) * 35 // 60
        say(f"⚠ 组合数较多（{len(tasks)} 个），预计约 {est_min} 分钟。加 --yes 跳过确认。")
        if not sys.stdin.isatty():
            say("非交互环境，加 --yes 确认执行。")
            return 0
        ans = input("继续？[y/N] ").strip().lower()
        if ans != "y":
            say("已取消")
            return 0

    # --json 只输出任务列表（dry-run），不执行
    if args.json:
        print(
            json.dumps(
                {
                    "schema_version": 1,
                    "command": "deep-dive",
                    "target": target,
                    "tasks": [
                        {"provider": p, "type": t, "model": m} for p, t, m in tasks
                    ],
                },
                ensure_ascii=False,
                indent=2,
            ),
            flush=True,
        )
        return 0

    say(f"深挖: {len(targets)} 家供应商 × {len(models)} 模型 = {len(tasks)} 个组合")
    script = str(Path(__file__).resolve())
    overall = 0
    for pname, ptype, model in tasks:
        say(f"\n{'=' * 60}")
        say(f"  深挖: {pname} ({ptype})  Model: {model}")
        say(f"{'=' * 60}")
        cmd = [
            sys.executable,
            script,
            "inspect",
            "--provider",
            pname,
            "--model",
            model,
            "--source",
            "manual",
            "--type",
            ptype,
            "--db",
            args.db,
            "--timeout",
            str(args.timeout),
            "--workers",
            "1",
            "--human",
        ]
        rc = subprocess.call(cmd)
        if rc != 0 and overall == 0:
            overall = 1
    return overall


def main():
    sys.argv = _inject_default_command(sys.argv)
    ap, *_ = _build_parser()
    args = ap.parse_args()

    if getattr(args, "quiet", False) and (
        getattr(args, "human", False) or getattr(args, "output_format", None) == "human"
    ):
        print(
            "--quiet 与 --human 互斥；忽略 --human，强制 JSON",
            file=sys.stderr,
            flush=True,
        )
        args.output_format = "json"
        args.human = False

    output_token = _output_stream.set(
        sys.stderr
        if getattr(args, "json", False)
        or (args.command == "inspect" and not getattr(args, "human", False))
        else sys.stdout
    )

    try:
        return _main_with_args(args)
    finally:
        _output_stream.reset(output_token)


def _main_with_args(args) -> int:
    """执行已解析参数；单独封装以保证输出 ContextVar 始终可恢复。"""
    quiet = getattr(args, "quiet", False)

    def _say_silent(*a, **k):
        pass

    out_say = _say_silent if quiet else say

    if getattr(args, "user_agent", None):
        say(f"User-Agent 已覆盖: {args.user_agent}")

    if args.skip_tls_verify:
        say("警告：已跳过 TLS 证书验证，认证凭据可能遭中间人截获。")

    # trend：读 CC-Pulse 自己的探测归档，不需要 cc-switch db
    if args.command == "trend":
        return run_trend(args, say)

    # deep-dive：从 check JSON 读，不需要加载 cc-switch providers（自己 subprocess 调 inspect）
    if args.command == "deep-dive":
        return run_deep_dive(args, say)

    if not Path(args.db).exists():
        say(f"数据库不存在: {args.db}")
        return 2

    # --archive 路径安全检查：复用 archive_path() 单一校验源，提前失败给清晰退出码
    if getattr(args, "archive", None):
        from ccpulse_archive import archive_path as _archive_path

        try:
            _archive_path(args.archive)
        except ValueError as exc:
            print(f"错误: {exc}", file=sys.stderr)
            return 2

    # 纯日志子命令：不加载 providers、不发 HTTP
    if args.command == "history":
        return run_history(args, say)
    if args.command == "stats":
        return run_stats(args, say)
    if args.command == "routing":
        return run_routing(args, say)
    if args.command == "watch":
        return run_watch(args, say)
    if args.command == "analyze":
        return run_analyze(args, say)
    if args.command == "health":
        return run_health(args, say)

    types = (
        ["claude", "codex", "openclaw"]
        if getattr(args, "type", "claude") == "all"
        else [getattr(args, "type", "claude")]
    )
    providers = []
    for t in types:
        providers.extend(load_providers(args.db, t))

    if getattr(args, "current_only", False) and providers:
        before = len(providers)
        providers = [p for p in providers if p.is_current]
        say(f"--current-only: {before} → {len(providers)}（只保留当前激活）")
    elif getattr(args, "failover_only", False) and providers:
        before = len(providers)
        providers = [p for p in providers if p.in_failover or p.is_current]
        say(f"--failover-only: {before} → {len(providers)}（只保留队列内+当前激活）")

    # --select: 交互式 TUI 选择供应商（非 TTY 自动降级到 --provider / 全量）
    if getattr(args, "select", False) and providers and not getattr(
        args, "compare", None
    ):
        from ccpulse_tui import select_providers

        indices = select_providers(providers)
        if indices is None:
            say("--select: 非 TTY 环境，忽略（改用 --provider 或全量检测）")
        elif not indices:
            say("--select: 未选择任何供应商，退出")
            return 2
        else:
            before = len(providers)
            providers = [providers[i] for i in indices]
            say(f"--select: {before} -> {len(providers)}（交互式选择）")

    # --compare 自带多 provider 目标，跳过全局 --provider 过滤以免裁掉对比对象
    if (
        getattr(args, "provider", None)
        and providers
        and not (args.command == "inspect" and getattr(args, "compare", None))
    ):
        prov_arg = str(args.provider).strip()
        sub_list = [s.strip().lower() for s in prov_arg.split(",") if s.strip()]
        if sub_list:
            before = len(providers)
            providers = [
                p for p in providers if any(sub in p.name.lower() for sub in sub_list)
            ]
            say(f"--provider '{prov_arg}': {before} → {len(providers)}（按名称过滤）")

    if args.command == "list-models":
        if not providers:
            say("没有符合条件的供应商")
            return 2
        return run_list_models(args, providers, say)

    if args.command == "check":
        if not providers:
            say("没有符合条件的供应商")
            return 2
        return run_health_check(args, providers, say)

    if args.command == "inspect":
        return run_inspect(args, providers, out_say)

    if args.command == "env-check":
        return run_env_check(args, providers, say)

    say(f"未知子命令: {args.command}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
