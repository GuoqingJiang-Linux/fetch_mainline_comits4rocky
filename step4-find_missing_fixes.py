#!/usr/bin/env python3
"""
检查 kernel_diffs/upstream_matched 中的每个上游 commit 是否在最新 Linux
内核中被后来的 commit 通过 "Fixes: <sha>" 修复；如果该修复 commit 本身
不在 upstream_matched 中，则输出它。

原理:
  1. 从每个 upstream_matched/*.patch 的 "From <sha>" 行取得该补丁对应
     的上游 commit。
  2. 默认先对 linux 仓库执行 `git pull --ff-only`，然后扫描
     v5.14..HEAD（最新 mainline）中所有非 merge 且包含 Fixes: 标签的 commit，把
     Fixes: 指向第 1 步集合的 (fix_commit, fixed_commit) 配对找出来。
     v5.14 之后的所有上游修复都应参与检查，因为 9.4 发布之后的修复正是
     “可能缺失”的候选。可用 --until 手动限制上界，--no-pull 跳过更新。
  3. 如果 fix_commit 不在第 1 步集合中，说明该修复没有出现在
     upstream_matched 里。
  4. 对 Rocky 9.4 base 冻结日（--base-cutoff，默认 2024-05-09）之前的
     fix commit，再检查它是否其实已经包含在 base 树中（反向 apply /
     行存在率）；已包含的从报告排除。cutoff 之后的修复不可能出现在
     9.4 GA 源码里，直接保留。

输出（每行是一条 fix_commit -> fixed_commit 关系）:
  upstream_matched_missing_fixes.tsv              全部缺失的修复
  upstream_matched_missing_fixes_same_file.tsv    仅保留修复 commit 与被修复
                                                  commit 改动过同一文件的条目
  *.sha1                                          去重后的 fix_commit 列表

TSV 列:
  fix_commit  fix_first_tag  fixed_commit  fixed_first_tag

其中 first_tag 由 `git describe --contains --match 'v*' <commit>` 得到，
取输出中最前面的内核版本 tag（例如 v6.9-rc1）。
"""
import os
import re
import argparse
import subprocess
import collections
import tempfile
import shutil
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.dirname(os.path.abspath(__file__))
PATCH_DIR = os.path.join(ROOT, "kernel_diffs", "upstream_matched")
LINUX_REPO = "/home/gjiang/sources/linux"
DEFAULT_START = "v5.14"
DEFAULT_FIX_EXCEPTIONS = os.path.join(ROOT, "step4-fix-exceptions.tsv")
# Rocky 9.4 base 树的冻结日期。早于该日期的修复才需要检查“是否其实已在
# base 中”；晚于该日期的修复不可能进入 9.4 GA 源码。注意：它只用于 base
# 检查，不限制修复 commit 的扫描范围（默认扫到最新 HEAD）。
DEFAULT_BASE_CUTOFF = "2024-05-09"

parser = argparse.ArgumentParser(
    description="找出 upstream_matched 中缺失的上游修复 commit。")
parser.add_argument("--repo", default=LINUX_REPO, help="Linux 源码仓库路径")
parser.add_argument("--until", default=None,
                    help="可选：只统计该日期之前的修复 commit；默认扫描到最新 HEAD")
parser.add_argument("--start", default=DEFAULT_START,
                    help=f"扫描起点，默认 {DEFAULT_START}")
parser.add_argument("--pull", action=argparse.BooleanOptionalAction, default=True,
                    help="扫描前在 linux 仓库执行 git pull --ff-only（默认开启，"
                         "可用 --no-pull 关闭）")
parser.add_argument("--base-tree", default="/home/gjiang/sources/linux-5.14.0-427.28.1.el9_4",
                    help="Rocky 9.4 源码树，用于判断修复是否已经包含在 9.4 中")
parser.add_argument("--base-cutoff", default=DEFAULT_BASE_CUTOFF,
                    help=f"只对早于该日期的修复做“是否已在 base 中”检查，"
                         f"默认 {DEFAULT_BASE_CUTOFF}")
parser.add_argument("--base-presence-ratio", type=float, default=0.80,
                    help="判定“修复已在 base 中”的行存在率阈值，默认 0.8")
parser.add_argument("--no-check-base-applied", action="store_true",
                    help="不排除“代码其实已经在 Rocky 9.4 base 树中”的修复 commit")
parser.add_argument("--fix-exceptions", default=DEFAULT_FIX_EXCEPTIONS,
                    help=f"人工确认无需报告的 fix commit 清单（默认: {DEFAULT_FIX_EXCEPTIONS}）")
args = parser.parse_args()
LINUX_REPO = args.repo

if not os.path.isdir(PATCH_DIR):
    raise SystemExit(f"找不到目录: {PATCH_DIR}")


def load_fix_exceptions(path):
    exceptions = {}
    if not os.path.isfile(path):
        raise SystemExit(f"找不到 fix commit 例外清单: {path}")
    with open(path, encoding="utf-8", errors="replace") as f:
        for line_num, line in enumerate(f, 1):
            line = line.rstrip("\r\n")
            if not line or line.lstrip().startswith("#"):
                continue
            parts = line.split("\t", 1)
            commit = parts[0].strip().lower()
            if not re.fullmatch(r"[0-9a-f]{40}", commit):
                raise SystemExit(
                    f"{path}:{line_num}: fix commit 必须是完整 40 位 SHA-1"
                )
            reason = parts[1].strip() if len(parts) == 2 else ""
            if not reason:
                raise SystemExit(f"{path}:{line_num}: 必须填写排除原因")
            exceptions[commit] = reason
    return exceptions


if args.pull:
    print("🔄 正在更新 linux 仓库 (git pull --ff-only) ...")
    r = subprocess.run(["git", "pull", "--ff-only"], cwd=LINUX_REPO,
                       capture_output=True, text=True)
    output = (r.stdout or "").strip() or (r.stderr or "").strip()
    print(f"   {output.replace(chr(10), chr(10) + '   ')}")
    if r.returncode != 0:
        print("⚠️ git pull 失败，继续使用当前 HEAD")

head_info = subprocess.run(
    ["git", "log", "-1", "--format=%H %ci %s", "HEAD"],
    cwd=LINUX_REPO, capture_output=True, text=True, check=True).stdout.strip()
print(f"📌 当前 linux HEAD: {head_info}")

if args.until:
    CUTOFF = subprocess.run(
        ["git", "rev-list", "--first-parent", "-1", f"--before={args.until}", "HEAD"],
        cwd=LINUX_REPO, capture_output=True, text=True, check=True).stdout.strip()
    if not CUTOFF:
        raise SystemExit(f"找不到 {args.until} 之前的 commit")
    RANGE = f"{args.start}..{CUTOFF}"
    cutoff_info = subprocess.run(
        ["git", "log", "-1", "--format=%ci %s", CUTOFF],
        cwd=LINUX_REPO, capture_output=True, text=True, check=True).stdout.strip()
    print(f"📅 修复 commit 扫描范围: {RANGE}")
    print(f"   {cutoff_info}")
else:
    RANGE = f"{args.start}..HEAD"
    print(f"📅 修复 commit 扫描范围: {RANGE}（最新 mainline）")

# ---------------------------------------------------------------- matched set
hash2patches = collections.defaultdict(list)
for fn in sorted(os.listdir(PATCH_DIR)):
    if not fn.endswith(".patch"):
        continue
    with open(os.path.join(PATCH_DIR, fn), errors="replace") as f:
        first = f.readline()
    m = re.match(r"From ([0-9a-f]{40}) ", first)
    if m:
        hash2patches[m.group(1)].append(fn)

matched = set(hash2patches)
print(f"upstream_matched 补丁数: {sum(len(v) for v in hash2patches.values())}, "
      f"去重后 commit 数: {len(matched)}")

prefix = collections.defaultdict(list)
for h in matched:
    for n in range(8, 41):
        prefix[h[:n]].append(h)

fixline_re = re.compile(r"^\s*[Ff]ixes:(.*)$", re.M)
hex_re = re.compile(r"\b([0-9a-fA-F]{6,40})\b")

# ---------------------------------------------------------------- scan Fixes:
print("扫描 linux 仓库中的 Fixes: 标签 ...")
log = subprocess.run(
    ["git", "log", RANGE, "--no-merges", "--regexp-ignore-case", "--grep=fixes:",
     "--format=%H%x1f%B%x1e"],
    cwd=LINUX_REPO, capture_output=True, text=True, check=True).stdout

edges = set()  # (fix_commit, fixed_commit)
refs = 0
for record in log.split("\x1e"):
    record = record.strip("\n")
    if not record:
        continue
    try:
        fix_commit, body = record.split("\x1f", 1)
    except ValueError:
        continue
    fix_commit = fix_commit.strip()
    if not re.fullmatch(r"[0-9a-f]{40}", fix_commit):
        continue
    for lm in fixline_re.finditer(body):
        # 只取括号前的部分，避免把 subject 里的类似 hash 的单词当 commit
        for hm in hex_re.finditer(lm.group(1).split("(", 1)[0]):
            sha = hm.group(1).lower()
            refs += 1
            if len(sha) >= 8:
                candidates = prefix.get(sha, [])
                if len(sha) == 40:
                    candidates = [sha] if sha in matched else []
            else:
                candidates = [h for h in matched if h.startswith(sha)]
            for fixed_commit in candidates:
                edges.add((fix_commit, fixed_commit))

print(f"共解析 {refs} 个 hash 引用, 命中已匹配 commit 的关系 {len(edges)} 条")

# ---------------------------------------------------------------- absent fixes
absent_edges = collections.defaultdict(set)
for fix_commit, fixed_commit in edges:
    if fix_commit not in matched:
        absent_edges[fix_commit].add(fixed_commit)

absent = sorted(absent_edges)
print(f"不在 upstream_matched 中的修复 commit: {len(absent)}")

# ---------------------------------------------------------------- manual exceptions
fix_exceptions = load_fix_exceptions(args.fix_exceptions)
absent_set = set(absent)
excluded_by_exception = absent_set & fix_exceptions.keys()
for commit in sorted(excluded_by_exception):
    print(f"例外清单排除 {commit[:12]}: {fix_exceptions[commit]}")
absent = [commit for commit in absent if commit not in fix_exceptions]
absent_edges = {commit: edges for commit, edges in absent_edges.items()
                if commit not in fix_exceptions}
unused_exceptions = set(fix_exceptions) - absent_set
if unused_exceptions:
    print(f"⚠️ 例外清单中有 {len(unused_exceptions)} 个 commit 未出现在当前缺失集合中")
print(f"应用例外清单后仍缺失的修复 commit: {len(absent)}")

# ---------------------------------------------------------------- metadata
def git(*args, stdin=None):
    return subprocess.run(["git", *args], cwd=LINUX_REPO, input=stdin,
                          capture_output=True, text=True, check=True).stdout

meta = {}
out = git("log", "--no-walk", "--stdin",
          "--format=%H%x1f%ci%x1f%an%x1f%s", stdin="\n".join(absent))
for line in out.splitlines():
    p = line.split("\x1f")
    if len(p) == 4:
        meta[p[0]] = p[1:]

all_fixed = sorted({c for cs in absent_edges.values() for c in cs})
fmeta = {}
if all_fixed:
    out = git("log", "--no-walk", "--stdin", "--format=%H%x1f%s",
              stdin="\n".join(all_fixed))
    for line in out.splitlines():
        p = line.split("\x1f", 1)
        if len(p) == 2:
            fmeta[p[0]] = p[1]

def commit_files(c):
    r = subprocess.run(
        ["git", "diff-tree", "--no-commit-id", "--name-only", "-r", c],
        cwd=LINUX_REPO, capture_output=True, text=True)
    return [x for x in r.stdout.splitlines() if x.strip()]

# ------------------------------------------------- base 已应用检查
# fix commit 可能已经在 Rocky 9.4 里，只是它的代码在 centos 和 base 中完全相同，
# 因此不会出现在 centos->base 的 diff 中，也就不会被 step3 收进 upstream_matched。
# 这里直接对 base 树做反向 apply / 行存在率检查，把这种“其实已修复”的 commit
# 从 missing 报告里排除。Rocky 9.4 没有 git log，只能这样验证。
_base_lines_cache = {}
_base_presence_ratio = args.base_presence_ratio
if _base_presence_ratio > 1.0:
    _base_presence_ratio /= 100.0


def valid_line(line):
    line = line.strip()
    return len(line) > 15 and not all(c in ' {}(};)[]\t\n\r' for c in line)


def base_file_lines(path):
    if path not in _base_lines_cache:
        if os.path.isfile(path):
            with open(path, errors="replace") as f:
                _base_lines_cache[path] = {l.strip() for l in f}
        else:
            _base_lines_cache[path] = None
    return _base_lines_cache[path]


def base_state(commit):
    """返回 (state, add_ratio, rem_ratio)。

    state:
      already_applied  反向 apply 成功，说明 base 里已有该修复
      not_applied      正向 apply 成功，说明 base 里没有该修复
      missing_file     base 中缺少该 commit 涉及的文件
      neither/both/empty  无法用 apply 判定，需要回退到行存在率
    """
    files = commit_files(commit)
    if not files:
        return "empty", 0.0, 0.0
    patch = git("show", "--format=", commit, "--", *files)
    added, removed = set(), set()
    for line in patch.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            x = line[1:].strip()
            if valid_line(x):
                added.add(x)
        elif line.startswith("-") and not line.startswith("---"):
            x = line[1:].strip()
            if valid_line(x):
                removed.add(x)

    add_ratio = rem_ratio = 0.0
    if added or removed:
        # 按“行是否出现在任意一个被改动文件的 base 版本中”统计，避免同一行
        # 在多个文件里被重复计数导致比例超过 100%。
        base_add = set()
        base_rem = set()
        for f in files:
            base = base_file_lines(os.path.join(args.base_tree, f))
            if base is None:
                continue
            base_add |= (added & base)
            base_rem |= (removed & base)
        add_ratio = len(base_add) / len(added) if added else 0.0
        rem_ratio = len(base_rem) / len(removed) if removed else 0.0

    # 用临时目录里的一份 base 文件做 apply --check（正向 / 反向）
    with tempfile.TemporaryDirectory() as tmp:
        for f in files:
            src = os.path.join(args.base_tree, f)
            if not os.path.isfile(src):
                return "missing_file", add_ratio, rem_ratio
            dst = os.path.join(tmp, f)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
        pf = os.path.join(tmp, ".fix.patch")
        with open(pf, "w") as f:
            f.write(patch)
        fwd = subprocess.run(["git", "apply", "--check", "-p1", pf],
                             cwd=tmp, capture_output=True, text=True)
        rev = subprocess.run(["git", "apply", "--check", "-R", "-p1", pf],
                             cwd=tmp, capture_output=True, text=True)
    if rev.returncode == 0 and fwd.returncode != 0:
        return "already_applied", add_ratio, rem_ratio
    if fwd.returncode == 0 and rev.returncode != 0:
        return "not_applied", add_ratio, rem_ratio
    if fwd.returncode == 0 and rev.returncode == 0:
        return "both", add_ratio, rem_ratio
    return "neither", add_ratio, rem_ratio


base_applied = set()
if not args.no_check_base_applied:
    # 只有 early 的修复才可能出现在 9.4 GA 源码里；cutoff 之后的修复
    # （正是我们要找的“发布后新增的缺失修复”）直接保留，省去大量检查。
    to_check = [c for c in absent
                if meta.get(c, ["9999-99-99"])[0][:10] < args.base_cutoff]
    skipped = len(absent) - len(to_check)
    print(f"检查 {len(to_check)} 个 {args.base_cutoff} 之前的修复 commit "
          f"是否已经包含在 Rocky 9.4 base 树中 "
          f"（{skipped} 个晚于 cutoff，直接保留）...")
    by_state = collections.Counter()
    for i, c in enumerate(to_check, 1):
        state, add_ratio, rem_ratio = base_state(c)
        by_state[state] += 1
        if state == "already_applied":
            base_applied.add(c)
        elif state in ("neither", "both", "empty"):
            # apply 无法判定时回退到行存在率：新增行大部分已在 base、且被删除的
            # 行大部分已不在 base，说明补丁效果已经存在。
            if (add_ratio >= _base_presence_ratio - 1e-9
                    and rem_ratio <= 1.0 - _base_presence_ratio + 1e-9):
                base_applied.add(c)
        if i % 200 == 0:
            print(f"  {i}/{len(to_check)}", flush=True)
    print("  " + ", ".join(f"{k}={v}" for k, v in sorted(by_state.items())))
    print(f"  已在 base 中、从报告排除: {len(base_applied)}")
    absent = [c for c in absent if c not in base_applied]
    absent_edges = {k: v for k, v in absent_edges.items() if k not in base_applied}
    print(f"排除后仍缺失的修复 commit: {len(absent)}")

# ---------------------------------------------------------------- exclusions (同 step1/step3)
step1 = os.path.join(ROOT, "step1-find_diffs.sh")
excludes = []
if os.path.exists(step1):
    text = open(step1).read()
    m = re.search(r"EXCLUDE_DIRS=\(([^)]*)\)", text)
    if m:
        excludes = [t.strip("\"'") for t in m.group(1).split("#", 1)[0].split()]
base_ex = {x for x in excludes if "/" not in x}
path_ex = [x.rstrip("/") for x in excludes if "/" in x]

def excluded(path):
    parts = path.split("/")
    if any(p in base_ex for p in parts):
        return True
    for pre in path_ex:
        if path == pre or path.startswith(pre + "/"):
            return True
    if len(parts) > 2 and parts[0] == "arch" and parts[1] not in ("x86", "arm64"):
        return True
    return False

# ---------------------------------------------------------------- reverts
revert_re = re.compile(r"This reverts commit ([0-9a-f]{40})")
reverted_by = collections.defaultdict(list)
out = git("log", RANGE, "--no-merges", "--grep=This reverts commit",
          "--format=%H%x1f%B%x1e")
for record in out.split("\x1e"):
    record = record.strip("\n")
    if not record:
        continue
    try:
        rev, body = record.split("\x1f", 1)
    except ValueError:
        continue
    for m in revert_re.finditer(body):
        reverted_by[m.group(1)].append(rev.strip())

# ---------------------------------------------------------------- build rows
fixed_files_cache = {}
rows = []
for fix_commit in absent:
    date, author, subject = meta.get(fix_commit, ["", "", ""])
    fixed = sorted(absent_edges[fix_commit])
    files = commit_files(fix_commit)
    shared_by_fixed = {}
    for c in fixed:
        if c not in fixed_files_cache:
            fixed_files_cache[c] = set(commit_files(c))
        shared_by_fixed[c] = sorted(set(files) & fixed_files_cache[c])
    shared = set()
    for v in shared_by_fixed.values():
        shared.update(v)
    rows.append({
        "fix": fix_commit,
        "date": date[:10].replace(" ", "T"),
        "author": author,
        "subject": subject,
        "fixed": fixed,
        "fixed_subjects": [fmeta.get(c, "") for c in fixed],
        "patches": [p for c in fixed for p in hash2patches.get(c, [])],
        "shared": sorted(shared),
        "shared_by_fixed": shared_by_fixed,
        "reverted_by": reverted_by.get(fix_commit, []),
        "inscope": [f for f in files if not excluded(f)],
        "files": files,
    })

rows.sort(key=lambda r: (";".join(r["patches"]), r["date"]))

# ---------------------------------------------------------------- kernel tags
tag_re = re.compile(r"^(v[^~^]+)")

def get_tag(commit):
    r = subprocess.run(
        ["git", "describe", "--contains", "--match", "v*", commit],
        cwd=LINUX_REPO, capture_output=True, text=True)
    if r.returncode != 0:
        return "(no tag)"
    m = tag_re.match(r.stdout.strip())
    return m.group(1) if m else r.stdout.strip()

commits = sorted({r["fix"] for r in rows} |
                 {c for r in rows for c in r["fixed"]})
print(f"查询 {len(commits)} 个 commit 的首个包含 tag ...")
tags = {}
with ThreadPoolExecutor(max_workers=4) as ex:
    for i, (c, t) in enumerate(zip(commits, ex.map(get_tag, commits)), 1):
        tags[c] = t
        if i % 500 == 0:
            print(f"  {i}/{len(commits)}", flush=True)

def abbrev_commit(sha):
    return sha[:12] if sha else sha

HEADER = ["fix_commit", "fix_first_tag", "fixed_commit", "fixed_first_tag"]

def edges_of(selected, only_shared=False):
    edges = set()
    for r in selected:
        for c in r["fixed"]:
            if only_shared and not r["shared_by_fixed"].get(c):
                continue
            edges.add((r["fix"], c))
    return sorted(edges, key=lambda e: (e[1], e[0]))

def write_report(path, selected, only_shared=False):
    edges = edges_of(selected, only_shared=only_shared)
    tmp = path + ".new"
    with open(tmp, "w") as f:
        for fix, fixed in edges:
            fix_subject = meta.get(fix, ["", "", ""])[2]
            fixed_subject = fmeta.get(fixed, "")
            f.write(f"fix {abbrev_commit(fix)}\t{tags.get(fix, '(no tag)')}\t{fix_subject}\n")
            f.write("the above fixed the below\n")
            f.write(f"fixed {abbrev_commit(fixed)}\t{tags.get(fixed, '(no tag)')}\t{fixed_subject}\n")
            f.write("\n")
    os.replace(tmp, path)
    with open(path[:-4] + ".sha1", "w") as f:
        for fix in sorted({r["fix"] for r in selected}):
            f.write(fix + "\n")
    print(f"已写入 {path}: {len(edges)} 条 ({len({r['fix'] for r in selected})} 个 fix_commit)")

all_out = os.path.join(ROOT, "upstream_matched_missing_fixes.tsv")
same_out = os.path.join(ROOT, "upstream_matched_missing_fixes_same_file.tsv")

same_rows = [r for r in rows if not r["reverted_by"]
             and any(r["shared_by_fixed"].get(c) for c in r["fixed"])]
write_report(all_out, rows)
write_report(same_out, same_rows, only_shared=True)

print("完成。")
