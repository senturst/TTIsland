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


def api(path: str, token: str) -> tuple[int, dict]:
    req = urllib.request.Request(
        "https://api.github.com" + path,
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
    if not perm.get("push"):
        print("  ✗ 该账号对此仓库没有写权限")
        return False

    # /user/installations 对 fine-grained token 是很好的权限探针：
    # 权限不足时返回 403，而这通常正是 git push 报 403 的同一个原因。
    st, inst = api("/user/installations", token)
    if st == 403:
        print("  ✗ token 未覆盖该仓库的写权限（/user/installations 返回 403）")
        print_perm_help()
        return False

    print("  ✓ 权限检查通过")
    return True


def print_perm_help() -> None:
    print(
        "\n  token 缺少写权限，两种改法（任选其一）：\n"
        "\n  【A】改现有 fine-grained token（推荐）\n"
        "       https://github.com/settings/personal-access-tokens\n"
        "       编辑该 token →\n"
        "         Repository access: Only select repositories → 勾选 TTIsland\n"
        "         Permissions → Repository permissions →\n"
        "             Contents ............ Read and write   ← 关键\n"
        "             Metadata ............ Read-only（自动勾选）\n"
        "       保存后 token 值不变，可直接重新推送。\n"
        "\n  【B】改用 classic token（更省事）\n"
        "       https://github.com/settings/tokens/new\n"
        "       勾选 repo 范围即可。\n"
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
