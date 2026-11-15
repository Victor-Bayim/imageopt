#!/usr/bin/env python3
import argparse
import json
from pathlib import Path


def load_json(path):
    if not path:
        return None
    p = Path(path)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def parse_args():
    parser = argparse.ArgumentParser(description="Build the LLM prompt used by oneclick.")
    parser.add_argument("--candidates", required=True)
    parser.add_argument("--selector-stats")
    parser.add_argument("--out", required=True)
    parser.add_argument("--output-contract", choices=("yaml", "json"), default="yaml")
    parser.add_argument("--budget-desc", required=True)
    parser.add_argument("--min-hits", type=int, required=True)
    parser.add_argument("--p0-max", type=int, required=True)
    parser.add_argument("--topn", type=int, default=400)
    return parser.parse_args()


def format_num(value, digits=3):
    if value is None:
        return "0"
    try:
        return f"{float(value):.{digits}f}"
    except Exception:
        return "0"


def normalize_path_kind(item):
    raw = str(item.get("path_kind") or "").strip().lower()
    if raw in ("regular", "directory", "other"):
        return raw
    if "is_regular_file" in item:
        return "regular" if bool(item.get("is_regular_file")) else "other"
    return ""


def candidate_line(item):
    aliases = item.get("alias_count")
    if aliases is None:
        aliases = len(item.get("aliases") or [item.get("path") or ""])
    return (
        f"{item.get('path', '')} | "
        f"size={int(item.get('size_bytes') or item.get('size') or 0)} | "
        f"count={format_num(item.get('weighted_count', item.get('count', 0)), 1)} | "
        f"first_ratio={format_num(item.get('first_ratio', 0.0), 3)} | "
        f"aliases={int(aliases or 1)} | "
        f"open_ratio={format_num(item.get('open_ratio', 0.0), 2)} | "
        f"mapfiles_ratio={format_num(item.get('mapfiles_ratio', 0.0), 2)} | "
        f"dict_ratio={format_num(item.get('dict_ratio', 0.0), 2)} | "
        f"maps_ratio={format_num(item.get('maps_ratio', 0.0), 2)} | "
        f"open_devino_ratio={format_num(item.get('open_devino_ratio', 0.0), 2)} | "
        f"confidence={format_num(item.get('confidence_score', 0.0), 2)}"
    )


def build_guard_section(stats):
    if not stats:
        return ""

    mode = str(stats.get("identity_guard_mode") or "normal")
    reason = str(stats.get("identity_guard_reason") or "identity coverage is sufficient")
    devino_ratio = format_num(stats.get("identity_paths_with_devino_ratio", 0.0), 3)
    effective_early_onehit_max = int(stats.get("effective_early_onehit_max") or 0)
    weak_maps_limit = format_num(stats.get("max_maps_ratio_without_strong_evidence", 0.8), 2)

    lines = [
        "当前 deterministic selector 审计摘要：",
        f"- identity_guard_mode: {mode}",
        f"- identity_guard_reason: {reason}",
        f"- identity_paths_with_devino_ratio: {devino_ratio}",
        f"- effective_early_onehit_max: {effective_early_onehit_max}",
        f"- max_maps_ratio_without_strong_evidence: {weak_maps_limit}",
    ]

    if mode == "normal":
        return "\n".join(lines)

    lines.append("")
    lines.append("样本可信度保护：")
    lines.append("- 当前样本的 identity 证据偏弱，优先相信 OPEN / mapfiles / dict / open_devino 明显更强的路径。")
    lines.append(f"- 对 maps_ratio >= {weak_maps_limit} 且缺少强证据的路径保持保守；拿不准时宁可不选。")
    if mode == "strict":
        lines.append("- 当前 deterministic selector 已禁用 early-onehit 扩展；不要主动补单次命中路径。")
    else:
        lines.append(
            f"- 当前 deterministic selector 只允许极少量 early-onehit 路径（最多 {effective_early_onehit_max} 条）；不要扩大这类选择。"
        )
    lines.append("- 不要为了凑满 P0/P1 列表而加入边缘路径。")
    return "\n".join(lines)


def build_prompt(candidates, stats, budget_desc, min_hits, p0_max, topn, output_contract):
    regular_candidates = [
        item for item in candidates if normalize_path_kind(item) in ("", "regular")
    ]
    top_candidates = regular_candidates[: max(topn, 0)]
    top_paths = "\n".join(candidate_line(item) for item in top_candidates)
    if not top_paths:
        top_paths = "<empty>"

    guard_section = build_guard_section(stats)
    prompt = (
        "你是一个容器镜像 eStargz prefetch list 优化助手。"
        "任务不是解释数据，而是根据真实冷启动采样信号，为 prefetch list 选择更合理的优先级。"
        "下面是进程冷启动期间实际被访问的 regular file 候选（已过滤 /proc、/sys、/dev、/run 噪声）：\n"
        f"{top_paths}\n\n"
    )
    if guard_section:
        prompt += guard_section + "\n\n"
    if output_contract == "json":
        prompt += (
            '最终输出必须且只能是一个严格 JSON 对象：{"P0":[],"P1":[],"P2":[],"P3":[]}。\n'
            "禁止事项：\n"
            "- 不允许输出 Markdown。\n"
            "- 不允许输出代码块。\n"
            "- 不允许输出解释、注释、标题、前后缀文本。\n"
            "- 不允许输出除该 JSON 对象外的任何内容。\n\n"
            "JSON 合同：\n"
            "- 顶层只能包含 4 个键：P0、P1、P2、P3。\n"
            "- 每个值都必须是字符串数组。\n"
            "- 数组元素必须是绝对文件路径字符串。\n"
            "- 路径必须来自上面的候选列表，不能编造新路径。\n"
            "- 不允许输出目录。\n"
            "- 不允许输出通配符或 glob（例如 *）。\n"
            "- 不允许输出 /proc、/sys、/dev、/run、/var/run 下的路径。\n\n"
            "优先级语义：\n"
            "- P0：启动最早期必须访问，极少且关键。\n"
            "- P1：紧随启动期访问，对 ready latency 有高影响。\n"
            "- P2：有一定价值，但不应挤占 P0/P1 预算。\n"
            "- P3：低优先级，或当前不建议预取。\n\n"
            "选择策略：\n"
            "- 优先 early access：first_ratio 越早越重要。\n"
            "- 优先访问次数高的路径，尤其是 count 更高者。\n"
            "- 优先 OPEN / MMAP / mapfiles / dict / open_devino 等多信号一致的路径。\n"
            "- 优先 confidence 更高的路径。\n"
            "- 谨慎选择超大文件；不要让少数大文件挤占 P0/P1 预算。\n"
            "- 不要因为路径名字看起来重要就选；必须以采样信号为主。\n"
            f"- P0 不超过 {p0_max} 条，P0+P1 的累计文件体积建议控制在 {budget_desc} 以内。\n"
            f"- 优先考虑访问次数 ≥ {min_hits}（OPEN+MMAP）的文件；对极小、单次、弱证据噪声保持保守。\n\n"
            "输出前自检（只在内部执行，不要出现在最终输出中）：\n"
            "- 检查最终是否只剩一个 JSON 对象。\n"
            "- 检查顶层键是否只有 P0/P1/P2/P3。\n"
            "- 检查每个值是否都是字符串数组。\n"
            "- 检查每条路径是否都来自候选列表、是绝对路径、不是目录、没有通配符。\n"
            "- 检查是否没有 Markdown、代码块、解释文字。\n"
        )
    else:
        prompt += (
            "请产出一个 YAML，键为 P0/P1/P2/P3 四个层级，值是各层级的绝对文件路径列表（单行一个）。\n"
            "要求：\n"
            "- P0：极少且关键（启动立即需要）\n"
            "- P1：很重要（紧随其后）\n"
            "- P2：一般重要\n"
            "- P3：低优先级\n"
            "- 只输出纯 YAML，且路径必须是绝对路径；不要包含通配符（*）或目录。\n"
            f"- P0 不超过 {p0_max} 条，P0+P1 的累计文件体积建议控制在 {budget_desc} 以内。\n"
            f"- 优先考虑访问次数 ≥ {min_hits}（OPEN+MMAP）的文件；忽略极小或仅出现一次的噪声。\n"
        )
    return prompt


def main():
    args = parse_args()
    candidates = load_json(args.candidates) or []
    stats = load_json(args.selector_stats)
    prompt = build_prompt(
        candidates=candidates,
        stats=stats,
        budget_desc=args.budget_desc,
        min_hits=args.min_hits,
        p0_max=args.p0_max,
        topn=args.topn,
        output_contract=args.output_contract,
    )
    Path(args.out).write_text(prompt, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
