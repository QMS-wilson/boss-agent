# -*- coding: utf-8 -*-
"""更新云端代码并重启服务（保留现有 cloud_config.json 与密钥）。"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sshtarget  # noqa: E402

HOST = sshtarget.HOST
USER = sshtarget.USER
LOCAL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # 项目根（跟随脚本位置，可整体搬迁）


def run(cli, cmd):
    code, out, err = sshtarget.run(cli, cmd, 60)
    return out.strip(), err.strip()


def main():
    cli = sshtarget.connect(timeout=15)
    sftp = cli.open_sftp()
    for name in os.listdir(os.path.join(LOCAL_ROOT, "cloudapi")):
        if name.endswith(".py"):
            sftp.put(os.path.join(LOCAL_ROOT, "cloudapi", name), f"/opt/v3cloud/cloudapi/{name}")
    sftp.put(os.path.join(LOCAL_ROOT, "cloudapi", "web", "admin.html"), "/opt/v3cloud/cloudapi/web/admin.html")
    sftp.close()
    run(cli, "systemctl restart v3cloud")
    time.sleep(2)
    out, _ = run(cli, "curl -s http://127.0.0.1:8000/api/v1/health")
    print(out)
    cli.close()


if __name__ == "__main__":
    import time

    main()
