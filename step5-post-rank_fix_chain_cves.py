#!/usr/bin/env python3
"""Associate CVE records and scores with every commit in step4-post fix chains."""

import argparse
import csv
from pathlib import Path

from cve_matching import add_cisa_scores, ensure_cve_repo, match_cves

ROOT = Path(__file__).resolve().parent
DEFAULT_INPUT = ROOT / "upstream_matched_missing_fixes_fix_chains.tsv"
DEFAULT_OUTPUT = ROOT / "upstream_matched_missing_fixes_fix_chains_cve.tsv"
DEFAULT_CVE_REPO = Path("~/sources/linux-kernel-vulns").expanduser()
DEFAULT_CISA_CACHE = Path("~/.cache/linux-cisa-cvss.json").expanduser()


def parse_args():
    parser = argparse.ArgumentParser(
        description="为 step4-post 修复链中的每个 commit 单独关联 CVE 和评分"
    )
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT,
                        help=f"修复链报告 (默认: {DEFAULT_INPUT})")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT,
                        help=f"CVE 关联报告 (默认: {DEFAULT_OUTPUT})")
    parser.add_argument("--cve-repo", type=Path, default=DEFAULT_CVE_REPO,
                        help="Linux CVE 数据库本地克隆目录")
    parser.add_argument("--cisa-cache", type=Path, default=DEFAULT_CISA_CACHE,
                        help=f"CISA 评分缓存文件 (默认: {DEFAULT_CISA_CACHE})")
    parser.add_argument("--refresh-cisa", action="store_true",
                        help="重新下载 CISA 评分并刷新缓存")
    return parser.parse_args()


def read_chains(path):
    with path.open(encoding="utf-8", newline="") as source:
        rows = list(csv.DictReader(source, delimiter="\t"))
    for row in rows:
        chain = [part.strip().lower() for part in row["fix_chain"].split("->")]
        if len(chain) < 3 or any(not part for part in chain):
            raise SystemExit(f"无效的 fix_chain: {row['fix_chain']}")
        row["chain_commits"] = chain
    return rows


def build_rows(chains, cves_by_commit):
    rows = []
    for chain in chains:
        commits = chain["chain_commits"]
        commit_scores = []
        cvss_scores = []
        for commit in commits:
            commit_detail = {
                "commit": commit,
                "CVE_scores": [],
            }
            cves = cves_by_commit.get(commit, {})
            for cve_id, scores in cves.items():
                cna = scores.get("cvss") or {}
                cvss_score = cna.get("score")
                if cvss_score is not None:
                    cvss_scores.append(cvss_score)
                cisa_score = scores.get("cisa_cvss")
                commit_detail["CVE_scores"].append({
                    "CVE": cve_id,
                    "CNA_CVSS_score": (
                        f"{cvss_score:.1f}" if cvss_score is not None else ""
                    ),
                    "CISA_score": (
                        f"{cisa_score['score']:.1f}" if cisa_score else ""
                    ),
                })
            commit_scores.append(commit_detail)

        rows.append({
            "chain": chain,
            "commit_scores": commit_scores,
            "top_cvss_score": max(cvss_scores) if cvss_scores else None,
        })

    rows.sort(key=lambda row: (
        row["top_cvss_score"] is None,
        -(row["top_cvss_score"] or 0),
        row["chain"]["fix_chain"],
    ))
    return rows


def write_report(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as output:
        writer = csv.writer(output, delimiter="\t", lineterminator="\n")
        writer.writerow([
            "rank", "root_matched_commit", "chain_depth", "fix_chain",
            "CVE_scores($commit-CVEScore:CISAScore)",
        ])
        for rank, row in enumerate(rows, 1):
            chain = row["chain"]
            commit_scores = []
            for commit in row["commit_scores"]:
                score_pairs = [
                    f"{cve['CNA_CVSS_score']}:{cve['CISA_score']}"
                    for cve in commit["CVE_scores"]
                ] or [":"]
                commit_scores.append(f"{commit['commit']}-{','.join(score_pairs)}")
            writer.writerow([
                rank,
                chain["root_matched_commit"],
                chain["chain_depth"],
                chain["fix_chain"],
                " ".join(commit_scores),
            ])


def main():
    args = parse_args()
    if not args.input.is_file():
        raise SystemExit(f"找不到修复链报告: {args.input}")

    chains = read_chains(args.input)
    if not chains:
        write_report(args.output, [])
        print(f"修复链: 0, 已写入空报告: {args.output}")
        return
    commits = {commit for chain in chains for commit in chain["chain_commits"]}
    published_dir = ensure_cve_repo(args.cve_repo)
    cves_by_commit = match_cves(published_dir, commits)
    add_cisa_scores(cves_by_commit, args.cisa_cache, args.refresh_cisa)
    rows = build_rows(chains, cves_by_commit)
    write_report(args.output, rows)

    cve_matches = sum(
        len(commit["CVE_scores"])
        for row in rows for commit in row["commit_scores"]
    )
    cisa_matches = sum(
        bool(cve["CISA_score"])
        for row in rows for commit in row["commit_scores"]
        for cve in commit["CVE_scores"]
    )
    print(f"修复链: {len(chains)}, 链上 commit: {len(commits)}")
    print(f"输出行: {len(rows)}, CVE 关联: {cve_matches}, CISA 评分: {cisa_matches}")
    print(f"已写入: {args.output}")


if __name__ == "__main__":
    main()
