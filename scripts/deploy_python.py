# -*- coding: utf-8 -*-
"""在目标服务器编译 Python 3.9.18（nohup + 日志轮询，防断连中断）。"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sshtarget  # noqa: E402

HOST = sshtarget.HOST
USER = sshtarget.USER

SH = r"""#!/bin/bash
exec >>/tmp/v3-deploy.log 2>&1
set -e
echo "[1/6] 安装 Development Tools ..."
yum groupinstall -y "Development Tools"
echo "[2/6] 安装编译依赖库 ..."
yum install -y zlib-devel bzip2-devel openssl-devel ncurses-devel sqlite-devel readline-devel libffi-devel
echo "[3/6] 下载 Python-3.9.18 ..."
cd /usr/local/src
curl -sSL -o Python-3.9.18.tgz https://www.python.org/ftp/python/3.9.18/Python-3.9.18.tgz
tar xzf Python-3.9.18.tgz
cd Python-3.9.18
echo "[4/6] configure ..."
./configure --prefix=/usr/local/python39 --with-ensurepip=install
echo "[5/6] make ..."
make -j2
echo "[6/6] make altinstall ..."
make altinstall
/usr/local/python39/bin/python3.9 --version
echo ALL-PYTHON-DONE
"""


def run(cli, cmd):
    code, out, err = sshtarget.run(cli, cmd, 30)
    return out.strip(), err.strip()


def main():
    cli = sshtarget.connect(timeout=15)

    for _ in range(40):
        out, _ = run(cli, "pgrep -c yum || echo 0")
        if out.strip() in ("0", "1"):
            break
        time.sleep(5)

    sftp = cli.open_sftp()
    with sftp.open("/tmp/v3-deploy.sh", "w") as fh:
        fh.write(SH)
    sftp.close()

    run(cli, "rm -f /tmp/v3-deploy.log; nohup bash /tmp/v3-deploy.sh >/dev/null 2>&1 & echo started")
    print("部署脚本已在后台启动，轮询日志…")

    deadline = time.time() + 25 * 60
    last = ""
    while time.time() < deadline:
        time.sleep(15)
        out, _ = run(cli, "tail -n 12 /tmp/v3-deploy.log 2>/dev/null")
        if out and out != last:
            print("----")
            print(out)
            last = out
        alive, _ = run(cli, "pgrep -f v3-deploy.sh >/dev/null && echo 1 || echo 0")
        done = "ALL-PYTHON-DONE" in out
        if done:
            print("编译完成。")
            cli.close()
            return
        if alive.strip() == "0" and not done:
            print("脚本异常退出，最后日志：")
            out2, _ = run(cli, "tail -n 40 /tmp/v3-deploy.log")
            print(out2)
            cli.close()
            raise SystemExit(1)
    raise SystemExit("超时未完成")


if __name__ == "__main__":
    main()
