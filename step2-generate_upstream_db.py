import os
import subprocess
import hashlib
import argparse
from multiprocessing import Pool, cpu_count

DEFAULT_REPO = "/home/gjiang/sources/linux"
DEFAULT_DB = "/home/gjiang/sources/get-mainline-commits-from-rocky/upstream_indexed.txt"
DEFAULT_START = "v5.14"
# Rocky Linux 9.4 GA 发布日。之后的 mainline commit 不可能被收进 Rocky 9.4，
# 因此不再扫描。可用 --until 覆盖（例如改成实际使用的 9.4 更新版本日期）。
DEFAULT_UNTIL = "2024-05-09"


def process_commit(args):
    commit_id, repo = args
    results = []
    # 调用 git show 提取该 commit 所有的变动行
    cmd = f"git show {commit_id}"
    process = subprocess.Popen(cmd, shell=True, cwd=repo,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    stdout, _ = process.communicate()

    for line_bytes in stdout.split(b'\n'):
        # 仅关注以 + 开头但不是 +++ 的新增代码行
        if line_bytes.startswith(b'+') and not line_bytes.startswith(b'+++'):
            try:
                line = line_bytes[1:].decode('utf-8', errors='ignore').strip()
            except Exception:
                continue

            # 过滤短行以及纯符号行（如纯大括号、纯分号、纯空行）
            if len(line) > 15 and not all(c in ' {}(};)[]\t\n\r' for c in line):
                h = hashlib.md5(line.encode('utf-8')).hexdigest()
                results.append(f"{h} {commit_id}\n")
    return results


def main():
    parser = argparse.ArgumentParser(
        description="生成 upstream commit 指纹库，只扫描到指定发布日期为止。")
    parser.add_argument("--repo", default=DEFAULT_REPO, help="Linux 源码仓库路径")
    parser.add_argument("--db", default=DEFAULT_DB, help="输出的指纹库文件")
    parser.add_argument("--start", default=DEFAULT_START,
                        help=f"扫描起点，默认 {DEFAULT_START}")
    parser.add_argument("--until", default=DEFAULT_UNTIL,
                        help=f"截止日期，默认 {DEFAULT_UNTIL}（Rocky 9.4 GA）")
    args = parser.parse_args()

    print(f"⏳ 正在确定 {args.until} 之前最后一个 mainline commit ...")
    cutoff = subprocess.check_output(
        ["git", "rev-list", "--first-parent", "-1",
         f"--before={args.until}", "HEAD"],
        cwd=args.repo, text=True).strip()
    if not cutoff:
        raise SystemExit(f"找不到 {args.until} 之前的 commit")

    cutoff_info = subprocess.check_output(
        ["git", "log", "-1", "--format=%ci %s", cutoff],
        cwd=args.repo, text=True).strip()
    print(f"📅 截止 commit: {cutoff}")
    print(f"   {cutoff_info}")

    print(f"⏳ 正在拉取上游 Linux ({args.start}..{cutoff}) 的提交记录列表...")
    cmd_list = ["git", "log", f"{args.start}..{cutoff}", "--format=%H"]
    commits = subprocess.check_output(
        cmd_list, cwd=args.repo, text=True).splitlines()
    total_commits = len(commits)
    print(f"📦 发现上游共计 {total_commits} 个 Commit 需要处理。")

    workers = cpu_count()
    print(f"🚀 正在利用 {workers} 个 CPU 核心并发提取指纹数据...")

    all_lines = []
    tasks = [(c, args.repo) for c in commits]
    with Pool(workers) as pool:
        # 流式并行处理各个 Commit
        for idx, res in enumerate(pool.imap_unordered(process_commit, tasks, chunksize=50)):
            all_lines.extend(res)
            if idx % 5000 == 0 and idx > 0:
                print(f"   已完成进度: {idx}/{total_commits} ...")

    print("💾 正在对海量指纹进行全局排序并写入缓存磁盘...")
    # 全局排序去重，保证秒级检索效率
    all_lines.sort()

    with open(args.db, "w") as f:
        f.writelines(all_lines)

    print(f"🎨 恭喜！上游指纹库构建成功，完美保存在: {args.db}")


if __name__ == "__main__":
    main()
