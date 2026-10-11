# -*- coding: utf-8 -*-
"""SSH 目标与认证的**唯一入口**（本仓 scripts/ 共用，三个仓内容一致）。

★ 为什么不留密码：同一个生产 root 密码曾被复制进 21 个 .py ——
  任何一个文件被备份、截图、贴进问答上下文都是一次泄露，
  把凭据收敛到「只有 ~/.ssh/config 和 env」才是修复，加一层封装只是形式。
  因此这里**没有任何兜底密码**：四条认证途径全失败就直接退出。

认证顺序（第一个能登上的就用）：
  1) env JXC_SSH_KEY 指定的私钥
  2) ~/.ssh/config 里该 Host 的 IdentityFile（paramiko.SSHConfig 解析）
  3) ~/.ssh/jxc_deploy
  4) env JXC_SSH_PASSWORD（仅临时救急，别写进任何文件）

环境变量：JXC_SSH_HOST / JXC_SSH_USER / JXC_SSH_KEY / JXC_SSH_PASSWORD
"""
import os

import paramiko

HOST = os.environ.get("JXC_SSH_HOST", "149.88.75.125")
USER = os.environ.get("JXC_SSH_USER", "root")
DEFAULT_KEY = os.path.expanduser("~/.ssh/jxc_deploy")


def _expand(path):
    return os.path.expandvars(os.path.expanduser(path))


def _config_identityfiles(host):
    """~/.ssh/config 为该 Host 解析出的、本地确实存在的私钥路径。"""
    cfg_path = _expand("~/.ssh/config")
    if not os.path.isfile(cfg_path):
        return []
    try:
        cfg = paramiko.SSHConfig()
        with open(cfg_path, encoding="utf-8") as fh:
            cfg.parse(fh)
        ids = cfg.lookup(host).get("identityfile") or []
    except Exception:
        return []
    return [p for p in (_expand(i) for i in ids) if os.path.isfile(p)]


def _auth_attempts(host):
    """[(说明, connect 的 kwargs)]，按优先级排列。"""
    attempts = []
    env_key = os.environ.get("JXC_SSH_KEY")
    if env_key and os.path.isfile(_expand(env_key)):
        attempts.append(("JXC_SSH_KEY=%s" % env_key, {"key_filename": _expand(env_key)}))
    for path in _config_identityfiles(host):
        attempts.append(("~/.ssh/config IdentityFile=%s" % path, {"key_filename": path}))
    if os.path.isfile(DEFAULT_KEY):
        attempts.append((DEFAULT_KEY, {"key_filename": DEFAULT_KEY}))
    if os.environ.get("JXC_SSH_PASSWORD"):
        attempts.append(("JXC_SSH_PASSWORD", {"password": os.environ["JXC_SSH_PASSWORD"]}))
    return attempts


def connect(timeout=25):
    """返回已登录的 paramiko.SSHClient；一个途径都没成就 SystemExit。"""
    tried = []
    for label, kw in _auth_attempts(HOST):
        cli = paramiko.SSHClient()
        # 主机键尚未 pin 死，先自动收录；agent 不参与（避免拿错身份）
        cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            cli.connect(HOST, username=USER, timeout=timeout, banner_timeout=timeout,
                        auth_timeout=timeout, allow_agent=False, look_for_keys=False, **kw)
            return cli
        except Exception as exc:
            tried.append("  - %s → %s: %s" % (label, type(exc).__name__, str(exc)[:200]))
            cli.close()
    raise SystemExit(
        "无法登录 %s@%s。\n%s\n请设 JXC_SSH_KEY=<私钥路径>（或临时用 JXC_SSH_PASSWORD），"
        "或确认 %s 存在且已在服务端 authorized_keys 里。"
        % (USER, HOST, "\n".join(tried) or "  - 没有可用凭据（env 与 ~/.ssh 都没找到密钥）",
           DEFAULT_KEY))


def run(cli, cmd, timeout=180):
    """执行远程命令，返回 (exit_code, stdout, stderr)，文本均为 utf-8/replace。"""
    _in, out, err = cli.exec_command(cmd, timeout=timeout)
    o = out.read().decode("utf-8", "replace")
    e = err.read().decode("utf-8", "replace")
    return out.channel.recv_exit_status(), o, e
