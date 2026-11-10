#!/usr/bin/env python3
"""Find follow-up Fixes: chains rooted at missing fixes from step4."""

import argparse
import csv
import os
import re
import subprocess
from bisect import bisect_left
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEFAULT_REPO = Path("/home/gjiang/sources/linux")
DEFAULT_INPUT = ROOT / "upstream_matched_missing_fixes.tsv"
DEFAULT_OUTPUT = ROOT / "upstream_matched_missing_fixes_fix_chains.tsv"
DEFAULT_MATCHED_DIR = ROOT / "kernel_diffs" / "upstream_matched"
FIXES_RE = re.compile(r"^\s*[Ff]ixes:(.*)$", re.MULTILINE)
HASH_RE = re.compile(r"\b([0-9a-fA-F]{8,40})\b")
FULL_HASH_RE = re.compile(r"[0-9a-f]{40}\Z")


def parse_args():
    parser = argparse.ArgumentParser(
        description="查找 step4 缺失修复报告中的后续 Fixes: 修复链"
    )
    parser.add_argument("--repo", type=Path, default=DEFAULT_REPO,
                        help=f"Linux 源码仓库 (默认: {DEFAULT_REPO})")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT,
                        help=f"step4 缺失修复报告 (默认: {DEFAULT_INPUT})")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT,
                        help=f"修复链输出文件 (默认: {DEFAULT_OUTPUT})")
    parser.add_argument("--matched-dir", type=Path, default=DEFAULT_MATCHED_DIR,
                        help=f"upstream_matched 补丁目录 (默认: {DEFAULT_MATCHED_DIR})")
    parser.add_argument("--start", default="v5.14",
                        help="Fixes: commit 扫描起点 (默认: v5.14)")
    parser.add_argument("--until", default=None,
                        help="可选扫描截止日期，格式如 2026-01-01")
    return parser.parse_args()


def git(repo, *args):
    return subprocess.run(
        ["git", *map(str, args)], cwd=repo, capture_output=True, text=True,
        check=True,
    ).stdout


def read_matched_commits(patch_dir):
    if not patch_dir.is_dir():
        raise SystemExit(f"找不到 upstream_matched 补丁目录: {patch_dir}")
    commits = set()
    for path in patch_dir.glob("*.patch"):
        try:
            first = path.open(encoding="utf-8", errors="replace").readline()
        except OSError as error:
            raise SystemExit(f"无法读取补丁 {path}: {error}")
        match = re.match(r"From ([0-9a-f]{40}) ", first)
        if match:
            commits.add(match.group(1))
    return commits


def read_seed_edges(path):
    edges = set()
    current_fix = None
    with path.open(encoding="utf-8", errors="replace") as report:
        for line in report:
            line = line.rstrip("\r\n")
            if line.startswith("fix "):
                fields = line[4:].split("\t", 2)
                current_fix = fields[0].lower() if fields else None
            elif line.startswith("fixed ") and current_fix:
                fields = line[6:].split("\t", 2)
                if fields:
                    edges.add((current_fix, fields[0].lower()))
    if not edges:
        raise SystemExit(f"未能从 step4 报告解析 Fixes 关系: {path}")
    return edges


def scan_fix_commits(repo, revision_range):
    log = git(
        repo, "log", revision_range, "--no-merges",
        "--regexp-ignore-case", "--grep=fixes:",
        "--format=%H%x1f%s%x1f%B%x1e",
    )
    records = {}
    for record in log.split("\x1e"):
        record = record.strip("\n")
        if not record:
            continue
        fields = record.split("\x1f", 2)
        if len(fields) != 3:
            continue
        commit, subject, body = fields
        if not FULL_HASH_RE.fullmatch(commit):
            continue
        refs = set()
        for match in FIXES_RE.finditer(body):
            refs.update(
                item.group(1).lower()
                for item in HASH_RE.finditer(match.group(1).split("(", 1)[0])
            )
        if refs:
            records[commit] = {"subject": subject, "refs": refs}
    return records


def resolve_prefix(prefix, sorted_commits):
    index = bisect_left(sorted_commits, prefix)
    if index == len(sorted_commits) or not sorted_commits[index].startswith(prefix):
        return None, False
    if index + 1 < len(sorted_commits) and sorted_commits[index + 1].startswith(prefix):
        return None, True
    return sorted_commits[index], False


def build_parent_map(records, known_commits):
    sorted_commits = sorted(known_commits)
    parents = defaultdict(set)
    resolution_cache = {}
    ambiguous = 0

    def resolve(prefix):
        nonlocal ambiguous
        if prefix not in resolution_cache:
            resolution_cache[prefix] = resolve_prefix(prefix, sorted_commits)
            if resolution_cache[prefix][1]:
                ambiguous += 1
        return resolution_cache[prefix][0]

    for fix, record in records.items():
        for prefix in record["refs"]:
            target = resolve(prefix)
            if target and target != fix:
                parents[target].add(fix)
    return parents, resolve, ambiguous


def collect_chains(seed_edges, parents, records, matched):
    chains = set()
    unresolved = set()
    for fix_prefix, root_prefix in seed_edges:
        fix, _ = resolve_prefix(fix_prefix, sorted(records))
        root, _ = resolve_prefix(root_prefix, sorted(matched))
        if not fix or not root:
            unresolved.add((fix_prefix, root_prefix))
            continue

        pending = [(fix,)]
        while pending:
            path = pending.pop()
            for later_fix in sorted(parents.get(path[0], ())):
                if later_fix in path:
                    continue
                extended = (later_fix,) + path
                if len(extended) >= 2:
                    chains.add((root, extended))
                pending.append(extended)
    return chains, unresolved


def write_chains(path, chains, records, matched):
    path.parent.mkdir(parents=True, exist_ok=True)
    ordered = sorted(chains, key=lambda item: (item[0], len(item[1]), item[1]))
    with path.open("w", encoding="utf-8", newline="") as output:
        writer = csv.writer(output, delimiter="\t", lineterminator="\n")
        writer.writerow([
            "root_matched_commit", "chain_depth", "fix_chain",
            "fix_chain_status", "fix_chain_subjects",
        ])
        for root, fixes in ordered:
            chain = (*fixes, root)
            statuses = [
                f"{commit[:12]}:{'matched' if commit in matched else 'missing'}"
                for commit in chain
            ]
            subjects = [
                f"{commit[:12]}: {records.get(commit, {}).get('subject', '')}"
                for commit in fixes
            ]
            writer.writerow([
                root[:12],
                len(fixes),
                " -> ".join(commit[:12] for commit in chain),
                "; ".join(statuses),
                " || ".join(subjects),
            ])
    return len(ordered)


def main():
    args = parse_args()
    if not args.input.is_file():
        raise SystemExit(f"找不到 step4 报告: {args.input}")
    if not args.repo.is_dir():
        raise SystemExit(f"找不到 Linux 仓库: {args.repo}")

    matched = read_matched_commits(args.matched_dir)
    seeds = read_seed_edges(args.input)
    if args.until:
        cutoff = git(
            args.repo, "rev-list", "--first-parent", "-1",
            f"--before={args.until}", "HEAD",
        ).strip()
        if not cutoff:
            raise SystemExit(f"找不到 {args.until} 之前的 commit")
        revision_range = f"{args.start}..{cutoff}"
    else:
        revision_range = f"{args.start}..HEAD"

    print(f"扫描非 merge Fixes: commit: {revision_range}")
    records = scan_fix_commits(args.repo, revision_range)
    known_commits = set(records) | matched
    parents, resolve, ambiguous_refs = build_parent_map(records, known_commits)
    chains, unresolved = collect_chains(seeds, parents, records, matched)

    count = write_chains(args.output, chains, records, matched)
    print(f"Fixes: commit: {len(records)}, 起始关系: {len(seeds)}")
    print(f"发现多级修复链: {count}, 未解析起始关系: {len(unresolved)}")
    if ambiguous_refs:
        print(f"跳过有歧义的 Fixes: hash 前缀: {ambiguous_refs}")
    print(f"已写入: {args.output}")


if __name__ == "__main__":
    main()
