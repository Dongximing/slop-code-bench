#!/usr/bin/env python3
"""Analyze agent trajectories from completed runs.

Extracts per-checkpoint stats for both inference and skill phases:
- Token usage (input, output, cache, reasoning)
- Steps, turns, tool calls
- Tool usage breakdown
- Cost

Usage:
    python scripts/analyze_trajectories.py <run_dir> [--csv] [--output <path>]

Examples:
    python scripts/analyze_trajectories.py outputs/glm-5-kimi/claude_code-2.0.51_just-solve_none_20260601T0247/
    python scripts/analyze_trajectories.py outputs/pangu/claude_code-2.0.51_just-solve_none_20260531T1813/ --csv --output results.csv
"""

import argparse
import json
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class TrajectoryStats:
    steps: int = 0
    turns: int = 0
    tool_calls: int = 0
    tools: Counter = field(default_factory=Counter)
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0
    cost: float = 0.0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def tools_summary(self) -> str:
        if not self.tools:
            return "-"
        return ", ".join(f"{name}:{count}" for name, count in self.tools.most_common())


def parse_stdout_jsonl(path: Path) -> TrajectoryStats:
    stats = TrajectoryStats()
    if not path.exists():
        return stats

    prev_role = None
    seen_msg_ids = set()

    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue

            msg_type = data.get("type", "")

            if msg_type == "result":
                usage = data.get("usage", {})
                stats.input_tokens = int(usage.get("input_tokens", 0))
                stats.output_tokens = int(usage.get("output_tokens", 0))
                stats.cache_read_tokens = int(usage.get("cache_read_input_tokens", 0))
                stats.cache_write_tokens = int(usage.get("cache_creation_input_tokens", 0))
                stats.cost = float(data.get("total_cost_usd", 0))
                stats.turns = int(data.get("num_turns", 0))
                continue

            message = data.get("message", {})
            if not isinstance(message, dict):
                continue

            role = message.get("role", "")
            msg_id = message.get("id", "")
            content = message.get("content", [])
            usage = message.get("usage", {})

            if role == "assistant":
                if usage and msg_id and msg_id not in seen_msg_ids:
                    input_t = int(usage.get("input_tokens", 0))
                    output_t = int(usage.get("output_tokens", 0))
                    if input_t > 0 or output_t > 0:
                        stats.steps += 1
                        seen_msg_ids.add(msg_id)

                if isinstance(content, list):
                    for item in content:
                        if isinstance(item, dict) and item.get("type") == "tool_use":
                            stats.tool_calls += 1
                            tool_name = item.get("name", "unknown")
                            stats.tools[tool_name] += 1

            if role and role != prev_role and role == "assistant":
                pass  # turns counted from result
            prev_role = role

    return stats


def parse_infer_log_skill(infer_log: Path, checkpoint_name: str) -> TrajectoryStats | None:
    """Parse skill trajectory from infer.log as fallback when after_skill/agent doesn't exist."""
    if not infer_log.exists():
        return None

    stats = TrajectoryStats()
    in_skill = False
    skill_started = False

    with open(infer_log) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue

            event = data.get("event", "")

            if f"Running post-checkpoint skill" in event and f"checkpoint='{checkpoint_name}'" in event:
                in_skill = True
                skill_started = True
                continue

            if in_skill:
                if "Post-checkpoint skill eval completed" in event or "Running post-checkpoint skill" in event:
                    if skill_started and "Post-checkpoint skill eval completed" in event:
                        break
                    continue

                msg_type = data.get("type", "")
                if msg_type == "assistant":
                    content = data.get("content", "")
                    if '"tool_use"' in content:
                        import re
                        tool_names = re.findall(r'"name":\s*"(\w+)"', content)
                        for name in tool_names:
                            stats.tool_calls += 1
                            stats.tools[name] += 1
                    stats.steps += 1

    if not skill_started:
        return None
    return stats


def analyze_checkpoint(ckpt_dir: Path, infer_log: Path, ckpt_name: str) -> dict:
    result = {"checkpoint": ckpt_name}

    # Before (inference)
    stdout_path = ckpt_dir / "agent" / "stdout.jsonl"
    before = parse_stdout_jsonl(stdout_path)

    inf_result = ckpt_dir / "inference_result.json"
    if inf_result.exists():
        with open(inf_result) as f:
            ir = json.load(f)
        usage = ir.get("usage", {})
        before.cost = usage.get("cost", 0)
        if before.steps == 0:
            before.steps = usage.get("steps", 0)
        net = usage.get("net_tokens", {})
        if net and before.input_tokens == 0:
            before.input_tokens = net.get("input", 0)
            before.output_tokens = net.get("output", 0)
            before.cache_read_tokens = net.get("cache_read", 0)
            before.cache_write_tokens = net.get("cache_write", 0)
            before.reasoning_tokens = net.get("reasoning", 0)

    result["before"] = before

    # After (skill)
    after_stdout = ckpt_dir / "after_skill" / "agent" / "stdout.jsonl"
    if after_stdout.exists():
        after = parse_stdout_jsonl(after_stdout)
    else:
        after = parse_infer_log_skill(infer_log, ckpt_name)

    result["after"] = after
    return result


def analyze_run(run_dir: Path) -> list[dict]:
    rows = []
    for prob_dir in sorted(run_dir.iterdir()):
        if not prob_dir.is_dir():
            continue
        infer_log = prob_dir / "infer.log"
        if not infer_log.exists():
            continue

        prob_name = prob_dir.name
        ckpt_idx = 1
        while True:
            ckpt_name = f"checkpoint_{ckpt_idx}"
            ckpt_dir = prob_dir / ckpt_name
            if not ckpt_dir.is_dir():
                break
            result = analyze_checkpoint(ckpt_dir, infer_log, ckpt_name)
            result["problem"] = prob_name
            rows.append(result)
            ckpt_idx += 1

    return rows


def print_markdown(rows: list[dict]) -> str:
    header = "| Problem | Ckpt | Phase | Steps | Turns | Tokens(in) | Tokens(out) | Reasoning | Tool Calls | Top Tools | Cost |"
    sep = "|---------|------|-------|-------|-------|------------|-------------|-----------|------------|-----------|------|"
    lines = [header, sep]

    for row in rows:
        prob = row["problem"]
        ckpt = row["checkpoint"]
        before = row["before"]
        after = row.get("after")

        top_tools = ", ".join(f"{n}:{c}" for n, c in before.tools.most_common(5)) or "-"
        lines.append(
            f"| {prob} | {ckpt} | before | {before.steps} | {before.turns} | "
            f"{before.input_tokens:,} | {before.output_tokens:,} | {before.reasoning_tokens:,} | "
            f"{before.tool_calls} | {top_tools} | ${before.cost:.2f} |"
        )

        if after:
            top_tools_a = ", ".join(f"{n}:{c}" for n, c in after.tools.most_common(5)) or "-"
            lines.append(
                f"| {prob} | {ckpt} | after | {after.steps} | {after.turns} | "
                f"{after.input_tokens:,} | {after.output_tokens:,} | {after.reasoning_tokens:,} | "
                f"{after.tool_calls} | {top_tools_a} | ${after.cost:.2f} |"
            )

    return "\n".join(lines)


def print_csv(rows: list[dict]) -> str:
    header = "problem,checkpoint,phase,steps,turns,input_tokens,output_tokens,cache_read,cache_write,reasoning_tokens,tool_calls,tools,cost"
    lines = [header]

    for row in rows:
        prob = row["problem"]
        ckpt = row["checkpoint"]
        before = row["before"]
        after = row.get("after")

        tools_str = "|".join(f"{n}:{c}" for n, c in before.tools.most_common())
        lines.append(
            f"{prob},{ckpt},before,{before.steps},{before.turns},{before.input_tokens},"
            f"{before.output_tokens},{before.cache_read_tokens},{before.cache_write_tokens},"
            f"{before.reasoning_tokens},{before.tool_calls},\"{tools_str}\",{before.cost:.4f}"
        )

        if after:
            tools_str_a = "|".join(f"{n}:{c}" for n, c in after.tools.most_common())
            lines.append(
                f"{prob},{ckpt},after,{after.steps},{after.turns},{after.input_tokens},"
                f"{after.output_tokens},{after.cache_read_tokens},{after.cache_write_tokens},"
                f"{after.reasoning_tokens},{after.tool_calls},\"{tools_str_a}\",{after.cost:.4f}"
            )

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Analyze agent trajectories from a run")
    parser.add_argument("run_dir", type=Path, help="Path to run directory")
    parser.add_argument("--csv", action="store_true", help="Output as CSV instead of markdown")
    parser.add_argument("--output", "-o", type=Path, help="Write output to file")
    args = parser.parse_args()

    if not args.run_dir.is_dir():
        print(f"Error: {args.run_dir} is not a directory", file=sys.stderr)
        sys.exit(1)

    rows = analyze_run(args.run_dir)
    if not rows:
        print("No trajectories found", file=sys.stderr)
        sys.exit(1)

    output = print_csv(rows) if args.csv else print_markdown(rows)

    if args.output:
        args.output.write_text(output)
        print(f"Written to {args.output}", file=sys.stderr)
    else:
        print(output)


if __name__ == "__main__":
    main()
