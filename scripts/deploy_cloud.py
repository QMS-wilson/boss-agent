# -*- coding: utf-8 -*-
"""上传并部署云端 API 到 149.88.75.125（systemd 常驻 + 随机新密钥）。"""

import io
import json
import os
import secrets
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sshtarget  # noqa: E402

HOST = sshtarget.HOST
USER = sshtarget.USER
LOCAL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # 项目根（跟随脚本位置，可整体搬迁）

ADMIN_PWD = secrets.token_urlsafe(12)
AGENT_SECRET = secrets.token_urlsafe(24)
CLIENT_TOKEN = secrets.token_hex(24)

CLOUD_CFG = {
    "server": {"host": "0.0.0.0", "port": 8000},
    "auth": {
        "clients": [{"token": CLIENT_TOKEN, "name": "shiwantongda-小程序", "agent": "shiwantongda"}],
        "agents": {"shiwantongda": AGENT_SECRET},
        "admin": {"password": ADMIN_PWD},
    },
    "default_agent": "shiwantongda",
    "rpc_timeout": 60,
    "log_dir": "/opt/v3cloud/logs",
}

SYSTEMD = """[Unit]
Description=V3 Cloud API
After=network.target

[Service]
Type=simple
WorkingDirectory=/opt/v3cloud
ExecStart=/opt/v3cloud/venv/bin/python -m cloudapi.main --config /opt/v3cloud/cloud_config.json
Restart=always
RestartSec=3
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
"""

INSTALL_SH = r"""#!/bin/bash
exec >>/tmp/v3-app.log 2>&1
set -e
echo "[1/4] venv ..."
/usr/local/python39/bin/python3.9 -m venv /opt/v3cloud/venv
echo "[2/4] pip install ..."
/opt/v3cloud/venv/bin/pip install --quiet 'fastapi>=0.110,<0.116' 'uvicorn>=0.29,<0.31' 'websockets>=12,<13'
echo "[3/4] systemd ..."
cp /tmp/v3cloud.service /etc/systemd/system/v3cloud.service
systemctl daemon-reload
systemctl enable v3cloud >/dev/null 2>&1
systemctl restart v3cloud
echo "[4/4] 本地健康检查 ..."
sleep 2
curl -s http://127.0.0.1:8000/api/v1/health || true
echo
echo ALL-APP-DONE
"""


def run(cli, cmd, timeout=40):
    code, out, err = sshtarget.run(cli, cmd, timeout)
    return out.strip(), err.strip()


def main():
    cli = sshtarget.connect(timeout=15)

    # 目录
    for d in ["/opt/v3cloud", "/opt/v3cloud/cloudapi", "/opt/v3cloud/cloudapi/web", "/opt/v3cloud/logs"]:
        run(cli, f"mkdir -p {d}")

    sftp = cli.open_sftp()
    for name in os.listdir(os.path.join(LOCAL_ROOT, "cloudapi")):
        if name.endswith(".py"):
            sftp.put(
                os.path.join(LOCAL_ROOT, "cloudapi", name),
                f"/opt/v3cloud/cloudapi/{name}",
            )
    sftp.put(
        os.path.join(LOCAL_ROOT, "cloudapi", "web", "admin.html"),
        "/opt/v3cloud/cloudapi/web/admin.html",
    )
    with sftp.open("/opt/v3cloud/cloud_config.json", "w") as fh:
        fh.write(json.dumps(CLOUD_CFG, ensure_ascii=False, indent=2))
    with sftp.open("/tmp/v3cloud.service", "w") as fh:
        fh.write(SYSTEMD)
    with sftp.open("/tmp/v3-install.sh", "w") as fh:
        fh.write(INSTALL_SH)
    sftp.close()

    run(cli, "rm -f /tmp/v3-app.log; nohup bash /tmp/v3-install.sh >/dev/null 2>&1 & echo started")
    print("安装脚本已后台启动，轮询日志…")

    deadline = time.time() + 8 * 60
    last = ""
    while time.time() < deadline:
        time.sleep(10)
        out, _ = run(cli, "tail -n 20 /tmp/v3-app.log 2>/dev/null")
        if out and out != last:
            print(out)
            last = out
        alive, _ = run(cli, "pgrep -f v3-install.sh >/dev/null && echo 1 || echo 0")
        if "ALL-APP-DONE" in out:
            break
        if alive.strip() == "0" and "ALL-APP-DONE" not in out:
            print("安装异常退出，日志尾部：")
            out2, _ = run(cli, "tail -n 50 /tmp/v3-app.log")
            print(out2)
            cli.close()
            raise SystemExit(1)
    else:
        cli.close()
        raise SystemExit("超时")

    # 防火墙检查/放行提示
    out, _ = run(cli, "iptables -L INPUT -n | head -n 12")
    print("---- iptables ----")
    print(out)

    # 保存凭据到本地（logs 目录已被 gitignore）
    cred_path = os.path.join(LOCAL_ROOT, "logs", "prod_credentials.json")
    with open(cred_path, "w", encoding="utf-8") as fh:
        json.dump(
            {
                "url": f"http://{HOST}:8000",
                "admin_page": f"http://{HOST}:8000/admin",
                "admin_password": ADMIN_PWD,
                "agent_name": "shiwantongda",
                "agent_secret": AGENT_SECRET,
                "client_token": CLIENT_TOKEN,
            },
            fh,
            ensure_ascii=False,
            indent=2,
        )
    print("凭据已保存到 logs/prod_credentials.json")
    print("ADMIN_PWD =", ADMIN_PWD)
    print("AGENT_SECRET =", AGENT_SECRET)
    print("CLIENT_TOKEN =", CLIENT_TOKEN)
    cli.close()


if __name__ == "__main__":
    main()
