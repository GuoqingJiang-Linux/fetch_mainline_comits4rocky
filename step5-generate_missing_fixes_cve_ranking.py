#!/usr/bin/env python3
"""Match missing fix commits to Linux CVE records and rank them by CVSS."""

import argparse
import csv
from pathlib import Path

from cve_matching import add_cisa_scores, ensure_cve_repo, match_cves

ROOT = Path(__file__).resolve().parent
DEFAULT_INPUT = ROOT / "upstream_matched_missing_fixes.tsv"
DEFAULT_OUTPUT = ROOT / "upstream_matched_missing_fixes_cve_ranked.tsv"
DEFAULT_CVE_REPO = Path("~/sources/linux-kernel-vulns").expanduser()
DEFAULT_CISA_CACHE = Path("~/.cache/linux-cisa-cvss.json").expanduser()


def parse_args():
    parser = argparse.ArgumentParser(
        description="将缺失修复关联到 CVE，按 CVSS 排序并补充 CISA 评分"
    )
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT,
                        help=f"缺失修复报告 (默认: {DEFAULT_INPUT})")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT,
                        help=f"排序后的报告 (默认: {DEFAULT_OUTPUT})")
    parser.add_argument("--cve-repo", type=Path, default=DEFAULT_CVE_REPO,
                        help="Linux CVE 数据库本地克隆目录；目录不存在时自动克隆")
    parser.add_argument("--cisa-cache", type=Path, default=DEFAULT_CISA_CACHE,
                        help=f"CISA 评分缓存文件 (默认: {DEFAULT_CISA_CACHE})")
    parser.add_argument("--refresh-cisa", action="store_true",
                        help="重新下载 CISA 评分并刷新缓存")
    return parser.parse_args()


def parse_report(path):
    fixes = {}
    current_fix = None
    with path.open(encoding="utf-8", errors="replace") as report:
        for line in report:
            line = line.rstrip("\r\n")
            if line.startswith("fix "):
                parts = line[4:].split("\t", 2)
                if len(parts) != 3:
                    continue
                commit, tag, subject = parts
                current_fix = commit.lower()
                record = fixes.setdefault(
                    current_fix,
                    {"tag": tag, "subject": subject, "fixed": set(), "occurrences": 0},
                )
                record["occurrences"] += 1
                if not record["subject"]:
                    record.update(tag=tag, subject=subject)
            elif line.startswith("fixed ") and current_fix:
                parts = line[6:].split("\t", 2)
                if parts:
                    fixes[current_fix]["fixed"].add(parts[0].lower())

    if not fixes:
        raise SystemExit(f"未能从报告中解析 fix commit: {path}")
    return fixes


def build_rows(fixes, cves_by_fix):
    rows = []
    for commit, info in fixes.items():
        cves = cves_by_fix.get(commit, {})
        if not cves:
            rows.append({
                "cve": "", "cvss": None, "fix": commit, "info": info,
            })
            continue
        for cve_id, scores in cves.items():
            cvss = scores["cvss"]
            rows.append({
                "cve": cve_id, "cvss": cvss, "fix": commit, "info": info,
                "cisa_cvss": scores["cisa_cvss"],
            })

    def sort_key(row):
        cvss = row["cvss"]
        if cvss:
            return (0, -cvss["score"], row["cve"], row["fix"])
        if row["cve"]:
            return (1, 0, row["cve"], row["fix"])
        return (2, 0, "", row["fix"])

    rows.sort(key=sort_key)
    return rows


def write_report(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as output:
        writer = csv.writer(output, delimiter="\t", lineterminator="\n")
        writer.writerow([
            "rank", "CVE", "CVSS分数", "严重性", "CISA评分",
            "fix_commit",
            "fix_commit是否重复", "fix_commit出现次数", "fix_tag", "fix_subject",
            "fixed_commit列表",
        ])
        for rank, row in enumerate(rows, 1):
            cvss = row["cvss"] or {}
            cisa_cvss = row.get("cisa_cvss") or {}
            cisa_score = ""
            if "score" in cisa_cvss:
                cisa_score = f"{cisa_cvss['score']:.1f}"
                if cisa_cvss.get("severity"):
                    cisa_score += f" ({cisa_cvss['severity']})"
            writer.writerow([
                rank,
                row["cve"],
                f"{cvss['score']:.1f}" if "score" in cvss else "",
                cvss.get("severity", ""),
                cisa_score,
                row["fix"],
                "是" if row["info"]["occurrences"] > 1 else "否",
                row["info"]["occurrences"],
                row["info"]["tag"],
                row["info"]["subject"],
                ",".join(sorted(row["info"]["fixed"])),
            ])


def main():
    args = parse_args()
    if not args.input.is_file():
        raise SystemExit(f"找不到输入报告: {args.input}")

    fixes = parse_report(args.input)
    published_dir = ensure_cve_repo(args.cve_repo)
    cves_by_fix = match_cves(published_dir, fixes)
    add_cisa_scores(cves_by_fix, args.cisa_cache, args.refresh_cisa)
    rows = build_rows(fixes, cves_by_fix)
    write_report(args.output, rows)

    scored = sum(bool(row["cvss"]) for row in rows)
    cve_matches = sum(bool(row["cve"]) for row in rows)
    duplicates = sum(info["occurrences"] > 1 for info in fixes.values())
    print(f"已生成: {args.output}")
    print(
        f"fix commits: {len(fixes)}, 匹配到CVE: {cve_matches}, "
        f"有CVSS评分: {scored}, 未匹配到CVE: {len(fixes) - cve_matches}, "
        f"重复fix commits: {duplicates}"
    )


if __name__ == "__main__":
    main()
