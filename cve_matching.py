"""Shared Linux CVE and CISA CVSS lookup helpers for step5 reports."""

import json
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

CVE_REPO_URL = "https://git.kernel.org/pub/scm/linux/security/vulns.git"
CVE_LIST_URL = "https://raw.githubusercontent.com/CVEProject/cvelistV5/main/cves"
CISA_ORG_ID = "134c704f-9b21-4f2e-91b3-4a467353bcc0"
COMMIT_RE = re.compile(r"[0-9a-f]{40}\Z", re.IGNORECASE)
CVSS_METRICS = (
    ("cvssV4_0", "4.0"),
    ("cvssV3_1", "3.1"),
    ("cvssV3_0", "3.0"),
    ("cvssV2_0", "2.0"),
)


def ensure_cve_repo(repo_path):
    repo_path = Path(repo_path).expanduser().resolve()
    published_dir = repo_path / "cve" / "published"
    if published_dir.is_dir():
        return published_dir
    if repo_path.exists():
        raise SystemExit(f"目录中找不到 Linux CVE 数据库: {published_dir}")

    repo_path.parent.mkdir(parents=True, exist_ok=True)
    print(f"正在克隆 Linux CVE 数据库到 {repo_path} ...", file=sys.stderr)
    result = subprocess.run(
        ["git", "clone", "--depth", "1", "--no-tags", CVE_REPO_URL, str(repo_path)],
        text=True, capture_output=True,
    )
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise SystemExit(f"克隆 Linux CVE 数据库失败: {detail}")
    if not published_dir.is_dir():
        raise SystemExit(f"克隆完成，但找不到 CVE 目录: {published_dir}")
    return published_dir


def get_cvss(data):
    containers = data.get("containers", {})
    cna = containers.get("cna", {})
    if not isinstance(cna, dict):
        return None
    for metric_name, version in CVSS_METRICS:
        for metrics in cna.get("metrics", []):
            metric = metrics.get(metric_name)
            if isinstance(metric, dict) and "baseScore" in metric:
                try:
                    score = float(metric["baseScore"])
                except (TypeError, ValueError):
                    continue
                return {
                    "version": metric.get("version", version),
                    "score": score,
                    "severity": metric.get("baseSeverity", ""),
                    "vector": metric.get("vectorString", ""),
                }
    return None


def get_cisa_cvss(data):
    containers = data.get("containers", {})
    adps = containers.get("adp", [])
    if isinstance(adps, dict):
        adps = [adps]
    for adp in adps:
        if not isinstance(adp, dict):
            continue
        provider = adp.get("providerMetadata", {})
        if (provider.get("orgId") != CISA_ORG_ID
                and provider.get("shortName") != "CISA-ADP"):
            continue
        for metric_name, version in CVSS_METRICS:
            for metrics in adp.get("metrics", []):
                metric = metrics.get(metric_name)
                if isinstance(metric, dict) and "baseScore" in metric:
                    try:
                        score = float(metric["baseScore"])
                    except (TypeError, ValueError):
                        continue
                    return {
                        "version": metric.get("version", version),
                        "score": score,
                        "severity": metric.get("baseSeverity", ""),
                        "vector": metric.get("vectorString", ""),
                    }
    return None


def fetch_cisa_cvss(cve_id):
    match = re.fullmatch(r"CVE-(\d{4})-(\d+)", cve_id)
    if not match:
        return False, None
    year, serial = match.groups()
    bucket = serial[:-3] + "xxx"
    url = f"{CVE_LIST_URL}/{year}/{bucket}/{cve_id}.json"
    request = Request(url, headers={"User-Agent": "step5-cve-ranking"})
    for attempt in range(3):
        try:
            with urlopen(request, timeout=30) as response:
                data = json.load(response)
            return True, get_cisa_cvss(data)
        except HTTPError as error:
            print(f"无法读取 {cve_id} 的 CVE 记录 (HTTP {error.code})", file=sys.stderr)
            return False, None
        except (URLError, TimeoutError, json.JSONDecodeError) as error:
            if attempt < 2:
                time.sleep(0.5 * (attempt + 1))
                continue
            print(f"无法读取 {cve_id} 的 CISA 评分: {error}", file=sys.stderr)
            return False, None


def load_cisa_cache(path):
    path = Path(path).expanduser()
    if not path.exists():
        return {}
    try:
        with path.open(encoding="utf-8") as cache_file:
            cache = json.load(cache_file)
    except (OSError, json.JSONDecodeError) as error:
        raise SystemExit(f"无法读取 CISA 评分缓存 {path}: {error}")
    if not isinstance(cache, dict):
        raise SystemExit(f"CISA 评分缓存格式无效: {path}")
    return cache


def add_cisa_scores(cves_by_fix, cache_path, refresh=False):
    cache_path = Path(cache_path).expanduser()
    cache = load_cisa_cache(cache_path)
    cve_ids = {
        cve_id
        for matches in cves_by_fix.values()
        for cve_id in matches
    }
    pending = sorted(cve_ids if refresh else cve_ids - cache.keys())
    if pending:
        print(f"正在获取 {len(pending)} 个 CVE 的 CISA 评分...", file=sys.stderr)
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {executor.submit(fetch_cisa_cvss, cve_id): cve_id
                       for cve_id in pending}
            for future in as_completed(futures):
                cve_id = futures[future]
                success, score = future.result()
                if success:
                    cache[cve_id] = score

        cache_path.parent.mkdir(parents=True, exist_ok=True)
        with cache_path.open("w", encoding="utf-8") as cache_file:
            json.dump(cache, cache_file, ensure_ascii=False, indent=2, sort_keys=True)
            cache_file.write("\n")

    for matches in cves_by_fix.values():
        for cve_id, scores in matches.items():
            scores["cisa_cvss"] = cache.get(cve_id) or scores["cisa_cvss"]


def match_cves(published_dir, commit_prefixes):
    cves_by_commit = {commit.lower(): {} for commit in commit_prefixes}
    for path in Path(published_dir).rglob("*.json"):
        try:
            with path.open(encoding="utf-8") as record_file:
                data = json.load(record_file)
        except (OSError, json.JSONDecodeError) as error:
            print(f"跳过无法读取的 CVE 记录 {path}: {error}", file=sys.stderr)
            continue

        cve_id = data.get("cveMetadata", {}).get("cveId")
        if not cve_id:
            continue
        cvss = get_cvss(data)
        cisa_cvss = get_cisa_cvss(data)
        containers = data.get("containers", {})
        for container in containers.values():
            if not isinstance(container, dict):
                continue
            for affected in container.get("affected", []):
                for version in affected.get("versions", []):
                    commit = version.get("lessThan", "")
                    if not isinstance(commit, str) or not COMMIT_RE.fullmatch(commit):
                        continue
                    prefix = commit[:12].lower()
                    if prefix in cves_by_commit and commit.lower().startswith(prefix):
                        cves_by_commit[prefix][cve_id] = {
                            "cvss": cvss,
                            "cisa_cvss": cisa_cvss,
                        }
    return cves_by_commit
