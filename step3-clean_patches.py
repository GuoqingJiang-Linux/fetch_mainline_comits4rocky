import os
import re
import sys
import subprocess
import hashlib
import tempfile
import shutil
import argparse
from collections import defaultdict

ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PATCH_DIR = os.path.expanduser("~/sources/get-mainline-commits-from-rocky/kernel_diffs")
DEFAULT_LINUX_REPO = "/home/gjiang/sources/linux"
# Rocky 9.4 的源码树（没有 git log，是判断 commit 是否真实存在的基准）
DEFAULT_BASE_TREE = "/home/gjiang/sources/linux-5.14.0-427.28.1.el9_4"
DEFAULT_INDEX = os.path.join(ROOT, "upstream_indexed.txt")
DEFAULT_RATIO = 0.80


def parse_ratio(value):
    """接受 0.8 或 80，统一转换成 0.8。"""
    try:
        v = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"无效比例: {value}")
    if v > 1.0:
        v /= 100.0
    if not (0.0 <= v <= 1.0):
        raise argparse.ArgumentTypeError(f"比例必须在 0~1 或 0~100 之间: {value}")
    return v


def valid_line(line):
    """与 step2 保持一致：过滤短行和纯符号行。"""
    line = line.strip()
    return len(line) > 15 and not all(c in ' {}(};)[]\t\n\r' for c in line)


def line_md5(line):
    return hashlib.md5(line.strip().encode('utf-8')).hexdigest()


class Cleaner:
    def __init__(self, args):
        self.args = args
        self.patch_dir = args.patch_dir
        self.matched_dir = os.path.join(self.patch_dir, "upstream_matched")
        os.makedirs(self.matched_dir, exist_ok=True)

        self.saved_commits = set()
        self._commit_files_cache = {}
        self._commit_added_cache = {}
        self._file_lines_cache = {}

        # 从 step1 解析 EXCLUDE_DIRS，保证与 step1 的过滤规则一致
        step1_script = os.path.join(ROOT, "step1-find_diffs.sh")
        self.basename_excludes = set()
        self.path_excludes = []
        if os.path.exists(step1_script):
            text = open(step1_script).read()
            m = re.search(r"EXCLUDE_DIRS=\(([^)]*)\)", text)
            if m:
                for name in [t.strip("\"'") for t in m.group(1).split("#", 1)[0].split()]:
                    if "/" in name:
                        self.path_excludes.append(name.rstrip("/"))
                    else:
                        self.basename_excludes.add(name)

        print("⏳ 正在加载上游指纹索引到内存...")
        self.upstream_db = {}
        if not os.path.exists(args.index):
            raise SystemExit(f"找不到指纹库: {args.index}")
        with open(args.index, "r") as f:
            for line in f:
                parts = line.strip().split(maxsplit=1)
                if len(parts) == 2:
                    lst = self.upstream_db.setdefault(parts[0], [])
                    if not lst or lst[-1] != parts[1]:
                        lst.append(sys.intern(parts[1]))

    # ------------------------------------------------------------------ git
    def get_commit_files(self, commit):
        if commit not in self._commit_files_cache:
            r = subprocess.run(
                ["git", "diff-tree", "--no-commit-id", "--name-only", "-r", commit],
                cwd=self.args.linux_repo, capture_output=True, text=True)
            self._commit_files_cache[commit] = (
                [l for l in r.stdout.splitlines() if l.strip()]
                if r.returncode == 0 else [])
        return self._commit_files_cache[commit]

    def get_commit_added_lines(self, commit, target_file):
        """返回 {md5: line}，只统计该 commit 在 target_file 中的有效新增行。"""
        key = (commit, target_file)
        if key in self._commit_added_cache:
            return self._commit_added_cache[key]

        cmd = ["git", "show", commit]
        if target_file:
            cmd += ["--", target_file]
        r = subprocess.run(cmd, cwd=self.args.linux_repo,
                           capture_output=True, text=True)
        lines = {}
        if r.returncode == 0:
            for line in r.stdout.splitlines():
                if line.startswith("+") and not line.startswith("+++"):
                    content = line[1:].strip()
                    if valid_line(content):
                        lines[line_md5(content)] = content
        self._commit_added_cache[key] = lines
        return lines

    # ------------------------------------------------------------------ files
    def get_file_lines(self, path):
        if path not in self._file_lines_cache:
            if os.path.isfile(path):
                with open(path, errors="replace") as f:
                    self._file_lines_cache[path] = {l.strip() for l in f}
            else:
                self._file_lines_cache[path] = set()
        return self._file_lines_cache[path]

    def is_excluded_path(self, path):
        parts = path.split("/")
        if any(p in self.basename_excludes for p in parts):
            return True
        for pre in self.path_excludes:
            if path == pre or path.startswith(pre + "/"):
                return True
        if len(parts) > 2 and parts[0] == "arch" and parts[1] not in ("x86", "arm64"):
            return True
        return False

    def get_target_file(self, header):
        m = re.search(r"^\+\+\+ (\S+)", header, re.M)
        if not m:
            return None
        path = m.group(1)
        for pre in ("linux-5.14.0-427.28.1.el9_4/", "centos-stream-9/"):
            if path.startswith(pre):
                return path[len(pre):]
        return path

    # ------------------------------------------------------------------ verify
    def base_presence(self, target_file, commit_lines):
        """该 commit 的有效新增行有多少比例真实存在于 Rocky 9.4 base 树。"""
        if not target_file or not commit_lines:
            return 0.0
        base = self.get_file_lines(os.path.join(self.args.base_tree, target_file))
        lines = set(commit_lines.values())
        return len(lines & base) / len(lines)

    def verify_commit(self, target_file, commit_lines, matched_md5s):
        """二次校验：真正触发匹配的 diff 行也必须能在 base 树中找到。

        这样可以拦住只靠 centos 侧（- 行）命中的 commit，避免把
        “只在 centos-stream-9 中存在”的 commit 当成 Rocky 9.4 的内容。
        """
        if not target_file or not commit_lines:
            return True, 1.0
        base = self.get_file_lines(os.path.join(self.args.base_tree, target_file))
        hit = matched_md5s & set(commit_lines)
        if not hit:
            return False, 0.0
        hit_lines = {commit_lines[h] for h in hit}
        ratio = len(hit_lines & base) / len(hit_lines)
        return ratio >= self.args.verify_ratio, ratio

    # ------------------------------------------------------------------ save
    def save_upstream_patch(self, commit):
        if commit in self.saved_commits:
            return
        self.saved_commits.add(commit)
        if self.args.dry_run:
            return
        with tempfile.TemporaryDirectory() as tmp:
            r = subprocess.run(
                ["git", "format-patch", "-1", commit, "-o", tmp],
                cwd=self.args.linux_repo, capture_output=True, text=True)
            if r.returncode != 0:
                print(f"format-patch 失败，跳过 {commit}: {r.stderr.strip()}")
                return
            names = os.listdir(tmp)
            if len(names) != 1:
                print(f"format-patch 输出异常，跳过 {commit}: {names}")
                return
            src = os.path.join(tmp, names[0])
            dst = os.path.join(self.matched_dir, names[0])
            if os.path.exists(dst):
                with open(src, "rb") as f1, open(dst, "rb") as f2:
                    if f1.read() == f2.read():
                        return
                base, ext = os.path.splitext(names[0])
                dst = os.path.join(self.matched_dir, f"{base}-{commit[:7]}{ext}")
            shutil.move(src, dst)

    # ------------------------------------------------------------------ patch
    def process_patch(self, patch_path, stats):
        with open(patch_path, "r", errors="ignore") as f:
            content = f.read()

        m0 = re.search(r"(?m)^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@", content)
        header = content[:m0.start()] if m0 else content
        hunk_texts = re.split(
            r"(?m)(?=^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@)",
            content[m0.start():]) if m0 else []
        target_file = self.get_target_file(header)

        hunk_infos = []
        # commit -> 本文件中命中的行 md5 集合
        matched_by_commit = defaultdict(set)

        for chunk in hunk_texts:
            md5s = set()
            for line in chunk.splitlines():
                if line.startswith("+") and not line.startswith("+++"):
                    if self.args.match_sides in ("plus", "both"):
                        text = line[1:].strip()
                        if valid_line(text):
                            md5s.add(line_md5(text))
                elif (self.args.match_sides in ("minus", "both")
                      and line.startswith("-") and not line.startswith("---")):
                    text = line[1:].strip()
                    if valid_line(text):
                        md5s.add(line_md5(text))

            hunk_infos.append({"chunk": chunk, "md5s": md5s})
            for h in md5s:
                for c in self.upstream_db.get(h, ()):
                    matched_by_commit[c].add(h)

        passing = set()
        for commit, matched_md5s in matched_by_commit.items():
            stats["candidates"] += 1
            files = self.get_commit_files(commit)
            if not files:
                stats["no_files"] += 1
                continue
            if target_file is not None and target_file not in files:
                stats["wrong_file"] += 1
                continue
            if all(self.is_excluded_path(f) for f in files):
                stats["excluded"] += 1
                continue

            commit_lines = self.get_commit_added_lines(commit, target_file)
            total = len(commit_lines)
            if total == 0:
                stats["empty_commit"] += 1
                continue

            matched = matched_md5s & set(commit_lines)
            # 80% 的口径：该 commit 的有效新增行中，至少 80% 必须真实存在于
            # Rocky 9.4 base 树的同一文件里。不能只看 diff 命中率，因为很多
            # 行在 centos 和 base 中完全相同，根本不会出现在 diff 里。
            ratio = self.base_presence(target_file, commit_lines)
            if ratio < self.args.min_match_ratio:
                stats["ratio_filtered"] += 1
                if self.args.verbose:
                    print(f"[base-ratio] {commit} {int(round(ratio * total))}/{total}"
                          f"={ratio:.1%} diff_hit={len(matched)} {target_file}")
                continue

            ok, v_ratio = self.verify_commit(target_file, commit_lines, matched_md5s)
            if not ok:
                stats["verify_filtered"] += 1
                if self.args.verbose:
                    print(f"[verify] {commit} base={ratio:.1%} "
                          f"trigger_in_base={v_ratio:.1%} {target_file}")
                continue

            passing.add(commit)
            stats["passed"] += 1
            if self.args.verbose:
                print(f"[keep] {commit} base={ratio:.1%} "
                      f"trigger_in_base={v_ratio:.1%} diff_hit={len(matched)} {target_file}")

        # 只丢弃“命中通过过滤的 commit”的 hunk
        kept_chunks = []
        for info in hunk_infos:
            remove = False
            for h in info["md5s"]:
                for c in self.upstream_db.get(h, ()):
                    if c in passing:
                        remove = True
                        break
                if remove:
                    break
            if remove:
                stats["hunks_removed"] += 1
            else:
                kept_chunks.append(info["chunk"])
                stats["hunks_kept"] += 1

        for commit in passing:
            self.save_upstream_patch(commit)

        if self.args.dry_run:
            return

        if kept_chunks:
            new_content = header + "".join(kept_chunks)
            if new_content != content:
                with open(patch_path, "w") as f:
                    f.write(new_content)
        else:
            os.remove(patch_path)
            stats["files_deleted"] += 1

    # ------------------------------------------------------------------ run
    def run(self):
        patch_files = sorted(f for f in os.listdir(self.patch_dir)
                             if f.endswith(".patch"))
        if self.args.limit:
            patch_files = patch_files[:self.args.limit]
        print(f"🚀 开始审计 {len(patch_files)} 个补丁文件 "
              f"(match_sides={self.args.match_sides}, "
              f"min_match_ratio={self.args.min_match_ratio:.0%}, "
              f"verify_ratio={self.args.verify_ratio:.0%}"
              f"{', dry-run' if self.args.dry_run else ''})...")

        stats = defaultdict(int)
        for i, pf in enumerate(patch_files, 1):
            patch_path = os.path.join(self.patch_dir, pf)
            if not os.path.isfile(patch_path):
                continue
            self.process_patch(patch_path, stats)
            if i % 1000 == 0:
                print(f"   已处理 {i}/{len(patch_files)} ...", flush=True)

        print("========================================")
        print(f"候选上游 commit 数: {stats['candidates']}")
        print(f"  文件不匹配:       {stats['wrong_file']}")
        print(f"  目录被排除:       {stats['excluded']}")
        print(f"  无有效新增行:     {stats['empty_commit']}")
        print(f"  base 存在率不足:  {stats['ratio_filtered']}")
        print(f"  二次校验过滤掉:   {stats['verify_filtered']}")
        print(f"  通过过滤:         {stats['passed']}")
        print(f"保留 hunk:          {stats['hunks_kept']}")
        print(f"丢弃 hunk:          {stats['hunks_removed']}")
        if not self.args.dry_run:
            print(f"删除文件:           {stats['files_deleted']}")
            print(f"成功提取上游补丁:   {len(os.listdir(self.matched_dir))}")
            print(f"剩余私有补丁文件:   {len([f for f in os.listdir(self.patch_dir) if f.endswith('.patch')])}")
        print("🎉 完成。" if not self.args.dry_run else "🧪 dry-run 完成，未修改任何文件。")


def main():
    parser = argparse.ArgumentParser(
        description="从 kernel_diffs 中剥离上游补丁，并按 Rocky 9.4 base 树的"
                    "实际存在率过滤低置信度匹配。")
    parser.add_argument("--patch-dir", default=DEFAULT_PATCH_DIR)
    parser.add_argument("--linux-repo", default=DEFAULT_LINUX_REPO)
    parser.add_argument("--base-tree", default=DEFAULT_BASE_TREE,
                        help="Rocky 9.4 源码树（判定 commit 是否真实存在的基准树）")
    parser.add_argument("--index", default=DEFAULT_INDEX,
                        help="step2 生成的 upstream_indexed.txt")
    parser.add_argument("--min-match-ratio", type=parse_ratio, default=DEFAULT_RATIO,
                        help="该 commit 的有效新增行必须至少有该比例真实出现在 Rocky 9.4 "
                             "base 树的同一文件中，默认 0.8（80%%）；也可以写 90 表示 90%%")
    parser.add_argument("--verify-ratio", type=parse_ratio, default=0.50,
                        help="二次校验：真正触发匹配的 diff 行也必须在 base 树中存在的"
                             "最低比例，默认 0.5（50%%）")
    parser.add_argument("--match-sides", choices=["plus", "minus", "both"], default="plus",
                        help="候选 commit 从 diff 的哪一侧产生：plus=只匹配 + 行"
                             "（base 独有内容，默认），minus=只匹配 - 行（centos 独有），"
                             "both=两侧都匹配")
    parser.add_argument("--dry-run", action="store_true",
                        help="只统计，不修改 kernel_diffs，也不生成 upstream_matched")
    parser.add_argument("--limit", type=int, default=0,
                        help="只处理前 N 个 patch 文件，便于测试")
    parser.add_argument("--verbose", action="store_true",
                        help="打印每个候选 commit 的匹配比例、二次校验结果和判定")
    args = parser.parse_args()

    Cleaner(args).run()


if __name__ == "__main__":
    main()
