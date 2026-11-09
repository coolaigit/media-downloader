# -*- coding: utf-8 -*-
"""把本地 V3.0.4 工作树发布到 GitHub：同步文件 → 打 tag → 建 Release → 传 exe。

为什么不用 git push：本仓库 2026-11-03 改写过历史后重新导入，本地 68 个提交与
远端 main 没有共同祖先；直接 push 会要求强推，把公开仓库已有历史抹掉。
这里改用 Git Data API，在远端 main 当前树上叠一个提交 —— 历史保留、一次提交、
只写「内容真的变了」的文件。

凭据从 `gh auth token` 取，不在脚本里存、不打印。
用法：_venv/Scripts/python.exe publish_release_v304.py [--sync-only]
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import subprocess
import sys
from pathlib import Path

import requests

OWNER = "coolaigit"
REPO = "media-downloader"
BRANCH = "main"
TAG = "v3.0.4"
RELEASE_NAME = "MediaDownloader V3.0.4"
ASSET_PATH = Path("_dist_ui/MediaDownloader_TEST.exe")
ASSET_NAME = "MediaDownloader_TEST.exe"
NOTES_PATH = Path("RELEASE_NOTES_V3.0.4.md")
COMMIT_MSG = ("release: V3.0.4 发布版（F-01~F-28 / CA 固化 / 会员试看提示 / 语言前缀）\n\n"
              "本地功能基线 aae5124，其后为发布说明、冻结台账与回归脚本入库。\n"
              "远端历史保留，不做强推。")

# 这些路径用远端版本，不用本地覆盖：
# .gitignore / .gitattributes 是开源瘦身时定的规则，本地那份是开发工作区版本；
# licenses/ 是第三方许可证原文，不该被本地行尾差异改写。
SKIP_EXACT = {".gitignore", ".gitattributes"}
SKIP_PREFIX = ("licenses/",)
BINARY_SUFFIX = {".ico", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".exe",
                 ".dll", ".zip", ".7z", ".pyc", ".pyd", ".pdf", ".woff", ".woff2"}


def git(*args: str) -> bytes:
    return subprocess.check_output(["git", *args])


def token() -> str:
    t = subprocess.check_output(["gh", "auth", "token"], text=True).strip()
    if not t:
        raise RuntimeError("gh auth token 为空，请先完成设备码授权")
    return t


class GitHub:
    def __init__(self, tok: str):
        self.s = requests.Session()
        self.s.headers.update({
            "Authorization": "Bearer " + tok,
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "media-downloader-release-script",
        })
        self.base = f"https://api.github.com/repos/{OWNER}/{REPO}"

    def req(self, method: str, url: str, **kw):
        kw.setdefault("timeout", 120)
        r = self.s.request(method, url if url.startswith("http") else self.base + url, **kw)
        if r.status_code >= 400:
            raise RuntimeError(f"{method} {url} -> {r.status_code}: {r.text[:500]}")
        return r.json() if r.content else {}

    def head(self) -> str:
        return self.req("GET", f"/git/ref/heads/{BRANCH}")["object"]["sha"]


def local_files() -> dict[str, tuple[str, bytes]]:
    """本地索引里的文本文件 → (mode, 规范化后的字节)。"""
    out: dict[str, tuple[str, bytes]] = {}
    raw = git("ls-files", "-s", "-z").decode("utf-8")
    for rec in raw.split("\0"):
        if not rec:
            continue
        meta, path = rec.split("\t", 1)
        mode, sha, _stage = meta.split()
        if path in SKIP_EXACT or path.startswith(SKIP_PREFIX):
            continue
        if Path(path).suffix.lower() in BINARY_SUFFIX:
            continue
        data = git("cat-file", "blob", sha)
        if b"\x00" in data[:8192]:
            continue
        out[path] = (mode, data)
    return out


def git_blob_sha(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sync-only", action="store_true", help="只同步文件，不建 tag/release")
    args = ap.parse_args()

    g = GitHub(token())
    head = g.head()
    tree = g.req("GET", f"/git/trees/{head}?recursive=1")
    if tree.get("truncated"):
        raise RuntimeError("远端 tree 被截断，改用分页接口")
    remote = {e["path"]: e["sha"] for e in tree["tree"] if e["type"] == "blob"}
    known_blobs = set(remote.values())

    files = local_files()
    changed = {p: v for p, v in files.items() if remote.get(p) != git_blob_sha(v[1])}
    added = [p for p in changed if p not in remote]
    print(f"[1/6] 本地文本 {len(files)} 个 / 远端 blob {len(remote)} 个 / "
          f"需写 {len(changed)} 个（新增 {len(added)}）", flush=True)
    for p in sorted(changed)[:12]:
        print("      ", ("+" if p in added else "~"), p, flush=True)
    if len(changed) > 12:
        print(f"       ... 其余 {len(changed) - 12} 个", flush=True)

    entries = []
    for i, (path, (mode, data)) in enumerate(sorted(changed.items()), 1):
        local_sha = git_blob_sha(data)
        if local_sha in known_blobs:
            blob_sha = local_sha  # 对象已在远端仓库里，直接引用
        else:
            blob_sha = g.req("POST", "/git/blobs", json={
                "content": base64.b64encode(data).decode("ascii"),
                "encoding": "base64",
            })["sha"]
            known_blobs.add(blob_sha)
        entries.append({"path": path, "mode": mode, "type": "blob", "sha": blob_sha})
        if i % 40 == 0:
            print(f"      blob {i}/{len(changed)}", flush=True)

    if entries:
        new_tree = g.req("POST", "/git/trees", json={
            "base_tree": tree["sha"], "tree": entries})["sha"]
        new_commit = g.req("POST", "/git/commits", json={
            "message": COMMIT_MSG, "tree": new_tree, "parents": [head]})["sha"]
        # 更新引用必须用复数 /git/refs/...；GET 单个引用才是单数 /git/ref/...
        g.req("PATCH", f"/git/refs/heads/{BRANCH}", json={"sha": new_commit, "force": False})
        print(f"[2/6] main 更新：{head[:8]} → {new_commit[:8]}（{len(entries)} 个文件）",
              flush=True)
    else:
        new_commit = head
        print("[2/6] 远端已是最新，跳过提交", flush=True)

    if args.sync_only:
        return 0

    # tag：已存在就复用，不重复建
    try:
        tag_ref = g.req("GET", f"/git/ref/tags/{TAG}")
        print(f"[3/6] tag {TAG} 已存在，复用", flush=True)
    except RuntimeError:
        tag_ref = g.req("POST", "/git/refs", json={
            "ref": f"refs/tags/{TAG}", "sha": new_commit})
        print(f"[3/6] tag {TAG} → {new_commit[:8]}", flush=True)

    body = NOTES_PATH.read_text(encoding="utf-8")
    releases = g.req("GET", "/releases?per_page=100")
    rel = next((r for r in releases if r["tag_name"] == TAG), None)
    if rel:
        rel = g.req("PATCH", f"/releases/{rel['id']}", json={"body": body})
        print(f"[4/6] Release 已存在，更新说明：{rel['html_url']}", flush=True)
    else:
        rel = g.req("POST", "/releases", json={
            "tag_name": TAG, "target_commitish": new_commit, "name": RELEASE_NAME,
            "body": body, "draft": False, "prerelease": False,
        })
        print(f"[4/6] Release 创建：{rel['html_url']}", flush=True)

    assets = g.req("GET", f"/releases/{rel['id']}/assets?per_page=100")
    if any(a["name"] == ASSET_NAME for a in assets):
        print("[5/6] 同名 asset 已存在，跳过上传", flush=True)
    else:
        if not ASSET_PATH.is_file():
            raise RuntimeError(f"找不到 release 文件：{ASSET_PATH}")
        size = ASSET_PATH.stat().st_size
        url = (f"https://uploads.github.com/repos/{OWNER}/{REPO}/"
               f"releases/{rel['id']}/assets?name={ASSET_NAME}")
        with ASSET_PATH.open("rb") as fh:
            r = g.s.post(url, data=fh, headers={
                "Content-Type": "application/octet-stream",
                "Content-Length": str(size),
            }, timeout=900)
        if r.status_code >= 400:
            raise RuntimeError(f"asset 上传失败 {r.status_code}: {r.text[:500]}")
        print(f"[5/6] asset 上传完成：{ASSET_NAME} {size:,} B / state="
              f"{r.json().get('state')}", flush=True)

    final = g.req("GET", f"/releases/{rel['id']}")
    final_assets = g.req("GET", f"/releases/{rel['id']}/assets?per_page=100")
    main_head = g.head()
    print("[6/6] 复核：")
    print("      main HEAD      :", main_head[:12])
    print("      tag            :", TAG, "→", tag_ref["object"]["sha"][:12])
    print("      release        :", final["html_url"])
    for a in final_assets:
        print("      asset          :", a["name"], a["size"], "downloads=",
              a["download_count"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
