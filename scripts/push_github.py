"""推送到 GitHub。

为什么不用 `git push` 直接推：
    把 token 写进远端 URL 会留存在 .git/config 里明文保存；
    从命令行传参又会进 shell 历史。这个脚本从 .env 读 token（.env 已被 gitignore），
    只在本次推送时拼接 URL，并在输出里屏蔽 token。

用法：
    1. 在 .env 里填 GITHUB_TOKEN=（见 .env.example 说明）
    2. python scripts/push_github.py            # 推当前分支到 origin
       python scripts/push_github.py --check    # 只检查 token 权限，不推送

### token 需要什么权限
GitHub 的 **fine-grained token 默认是 "Public repositories (read-only)"**，
即使仓库是你自己的，用它推送也会得到 403 Permission denied。
需要改成：
    设置 → Developer settings → Personal access tokens → Fine-grained tokens
    → Repository access: Only select repositories → 勾选 TTIsland
    → Permissions → Repository permissions → **Contents: Read and write**
或者直接用 Classic token，勾选 `repo` 范围（更省事）。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPO = "senturst/TTIsland"


def load_token() -> str:
    """优先环境变量，其次 .env。顺序不能反——环境变量是更明确的意图。"""
    tok = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""
    if tok.strip():
        return tok.strip()

    env_file = ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            if k.strip() in ("GITHUB_TOKEN", "GH_TOKEN") and v.strip():
                return v.strip()
    return ""


def mask(text: str, token: str) -> str:
    return text.replace(token, "***") if token else text


def api(path: str, token: str, method: str = "GET", body: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        "https://api.github.com" + path,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "ttisland-push",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"_raw": raw[:200]}
    except Exception as exc:  # noqa: BLE001
        return 0, {"_err": f"{type(exc).__name__}: {exc}"}


def preflight(token: str) -> bool:
    """推送前先查清楚身份和权限——比直接推失败再排查快得多。"""
    print("检查 token …")
    st, me = api("/user", token)
    if st != 200:
        print(f"  ✗ token 无效或已过期（HTTP {st}）")
        print(f"    {me}")
        return False
    login = me.get("login")
    kind = "fine-grained" if token.startswith("github_pat_") else "classic"
    print(f"  ✓ 身份: {login}（{kind} token）")

    st, repo = api(f"/repos/{REPO}", token)
    if st != 200:
        print(f"  ✗ 读不到仓库 {REPO}（HTTP {st}）")
        return False
    perm = repo.get("permissions") or {}
    print(f"  ✓ 仓库: {repo.get('full_name')}（{'私有' if repo.get('private') else '公开'}）")
    print(f"    默认分支: {repo.get('default_branch')}")

    # 仓库为空时没有 main ref，返回 409 —— 这是正常情况，不是错误
    st_ref, _ = api(f"/repos/{REPO}/git/ref/heads/{repo.get('default_branch')}", token)
    if st_ref == 409:
        print("    仓库当前为空（尚无任何提交）")

    # 注意：/repos 返回的 permissions 反映的是**账号**对该仓库的权限，
    # 而不是这个 token 被授予的权限。许多 fine-grained token 在这里显示
    # push=true，实际 push 仍然 403 —— 所以这里只作参考，真正的判据是能否推送。
    print(f"    账号权限: push={perm.get('push')}（注意：这是账号权限，不等于 token 权限）")
    if not perm.get("push"):
        print("  ✗ 该账号对此仓库没有写权限")
        return False

    # 精确探针：创建一个 dangling blob（只写对象、不建引用，不可见且会被回收）。
    # 它需要 Contents:write，是判断 token 是否真的有写权限的可靠手段——
    # /repos 返回的 permissions 反映的是账号权限，会给出误导性的 push=true。
    st_blob, blob = api(f"/repos/{REPO}/git/blobs", token, "POST",
                        {"content": "cHJvYmU=", "encoding": "base64"})
    if st_blob in (201, 409):
        # 201 = 写入成功；409 = 仓库为空、但权限已通过校验
        print("  ✓ Contents:write 已生效")
        return True
    if st_blob == 403:
        msg = (blob.get("message") or "").lower()
        print("  ✗ token 仍是只读的")
        if "not accessible" in msg:
            print("    （Resource not accessible by personal access token）")
        print_perm_help()
        return False

    print(f"  ? 探针返回 {st_blob}，无法判定，将继续尝试推送")
    return True


def print_perm_help() -> None:
    print(
        "\n  【最常见的原因】只改了 Permissions，但没改上面的 Repository access。\n"
        "  fine-grained token 创建时默认是 \"Public repositories (read-only)\"，\n"
        "  这个模式下 Permissions 区**根本不生效**——看起来改了，实际还是只读。\n"
        "\n  【A】修好现有 fine-grained token（token 值不变）\n"
        "       https://github.com/settings/personal-access-tokens\n"
        "       点开该 token，从上往下检查两处：\n"
        "         1. Repository access（在页面上半部分，关键！）\n"
        "              选 Only select repositories → 勾上 TTIsland\n"
        "              或选 All repositories\n"
        "            ← 若这里还是 \"Public repositories (read-only)\"，下面改了也没用\n"
        "         2. Permissions → Repository permissions →\n"
        "              Contents ......... Read and write   ← 必须显式改这一项\n"
        "              Metadata ......... Read-only（勾了 Contents 后自动带出）\n"
        "       最后点页面底部的 Save。改完 token 值不变，直接重新推送。\n"
        "\n  【B】改用 classic token（更省事，推荐给不想折腾的）\n"
        "       https://github.com/settings/tokens/new\n"
        "       勾选 repo 范围即可，没有 Repository access 这层坑。\n"
        "       生成后把新 token 填进 .env 的 GITHUB_TOKEN= 再跑本脚本。\n"
    )


def run(cmd: list[str], token: str) -> tuple[int, str]:
    env = dict(os.environ)
    # 绕开这台机器上 PortableGit 系统配置的锁文件问题，
    # 同时确保不把凭据交给任何 credential helper（避免 token 落盘）
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_TERMINAL_PROMPT"] = "0"
    p = subprocess.run(cmd, cwd=ROOT, capture_output=True, env=env)
    out = (p.stdout + p.stderr).decode("utf-8", "replace")
    return p.returncode, mask(out, token)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只检查权限，不推送")
    ap.add_argument("--branch", default=None, help="要推送的分支，默认当前分支")
    args = ap.parse_args()

    token = load_token()
    if not token:
        print("未找到 token。请在 .env 里填 GITHUB_TOKEN=…（该文件已被 gitignore）")
        return 1

    if not preflight(token):
        return 1
    if args.check:
        print("\n权限检查通过，可以推送。")
        return 0

    rc, branch = run(["git", "branch", "--show-current"], "")
    branch = (args.branch or branch).strip() or "main"

    print(f"\n推送 {branch} → origin …")
    url = f"https://senturst:{token}@github.com/{REPO}.git"
    rc, out = run(
        ["git", "-c", "credential.helper=", "push", url, f"{branch}:{branch}"], token
    )
    print(mask(out.strip(), token))

    if rc == 0:
        print(f"\n✓ 推送成功：https://github.com/{REPO}")
        return 0
    if "403" in out or "denied" in out.lower():
        print_perm_help()
    return rc


if __name__ == "__main__":
    sys.exit(main())
