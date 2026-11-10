#!/usr/bin/env python3
"""Merge remaining per-file diffs and print aggregate statistics."""

import shutil
import tempfile
from collections import defaultdict
from pathlib import Path, PurePosixPath


ROOT = Path(__file__).resolve().parent
PATCH_DIR = ROOT / "kernel_diffs"
OUTPUT = ROOT / "kernel_diffs_merged.patch"


def source_path(header):
    path = header[4:].rstrip(b"\r\n").split(b"\t", 1)[0]
    if path == b"/dev/null":
        return None
    tree, separator, relative_path = path.partition(b"/")
    if separator and (tree == b"centos-stream-9" or tree.startswith(b"linux-")):
        path = relative_path
    return path.decode("utf-8", errors="replace")


def main():
    if not PATCH_DIR.is_dir():
        raise SystemExit(f"找不到补丁目录: {PATCH_DIR}")

    patches = sorted(path for path in PATCH_DIR.glob("*.patch") if path.is_file())
    additions = 0
    deletions = 0
    diff_lines = 0
    directory_stats = defaultdict(lambda: [0, 0])

    with tempfile.TemporaryFile() as merged_body:
        for patch in patches:
            in_hunk = False
            last_line = None
            old_path = None
            new_path = None
            patch_additions = 0
            patch_deletions = 0
            with patch.open("rb") as source:
                for line in source:
                    diff_lines += 1
                    if not in_hunk and line.startswith(b"--- "):
                        old_path = source_path(line)
                    elif not in_hunk and line.startswith(b"+++ "):
                        new_path = source_path(line)
                    if line.startswith(b"@@ "):
                        in_hunk = True
                    if in_hunk and line.startswith(b"+"):
                        additions += 1
                        patch_additions += 1
                    elif in_hunk and line.startswith(b"-"):
                        deletions += 1
                        patch_deletions += 1
                    merged_body.write(line)
                    last_line = line
                if last_line and not last_line.endswith(b"\n"):
                    merged_body.write(b"\n")
            relative_path = new_path or old_path
            if relative_path is None:
                raise SystemExit(f"无法从补丁识别文件路径: {patch}")
            directory = str(PurePosixPath(relative_path).parent)
            directory_stats[directory][0] += patch_additions
            directory_stats[directory][1] += patch_deletions

        sorted_directories = sorted(
            directory_stats.items(),
            key=lambda item: (-(item[1][0] + item[1][1]), item[0]),
        )
        with OUTPUT.open("wb") as merged:
            summary = [
                "# kernel_diffs_merged.patch summary\n",
                f"# patch files: {len(patches)}\n",
                f"# added lines: {additions}\n",
                f"# deleted lines: {deletions}\n",
                f"# diff lines (excluding summary; including patch headers and context): "
                f"{diff_lines}\n",
                "# directory statistics, sorted by added + deleted lines\n",
                "# directory\tadded\tdeleted\ttotal\n",
            ]
            for directory, (added, removed) in sorted_directories:
                summary.append(
                    f"# {directory}\t{added}\t{removed}\t{added + removed}\n"
                )
            summary.append("#\n")
            merged.writelines(line.encode("utf-8") for line in summary)
            merged_body.seek(0)
            shutil.copyfileobj(merged_body, merged)

    print(f"合并文件: {OUTPUT}")
    print(f"patch 文件数: {len(patches)}")
    print(f"新增行数: {additions}")
    print(f"删除行数: {deletions}")
    print(f"diff 总行数: {diff_lines}")
    print("目录统计（按新增与删除行数合计降序）:")
    print("目录\t新增\t删除\t合计")
    for directory, (added, removed) in sorted_directories:
        print(f"{directory}\t{added}\t{removed}\t{added + removed}")


if __name__ == "__main__":
    main()
