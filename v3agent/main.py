# -*- coding: utf-8 -*-
"""命令行入口：
  py -m v3agent.main --config config.json --console
  py -m v3agent.main --config config.json --once '{"op":"bill.get","params":{"bill_no":"XK00000039"}}'
  py -m v3agent.main --config config.json --ws
"""

import argparse
import json
import logging
import os
import sys
import tempfile
import time

from . import config as cfgmod
from .agent import LocalAgent, run_console, run_http, run_ws


def _setup_logging(cfg):
    level = getattr(logging, str(cfg.get("log_level", "INFO")).upper(), logging.INFO)
    handlers = []
    if not getattr(sys, "frozen", False):
        handlers.append(logging.StreamHandler())
    try:
        base = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else os.getcwd()
        log_dir = os.path.join(base, "logs")
        os.makedirs(log_dir, exist_ok=True)
        handlers.append(logging.FileHandler(os.path.join(log_dir, "agent.log"), encoding="utf-8"))
    except Exception:
        pass
    logging.basicConfig(level=level, handlers=handlers or None,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")


def main():
    stage = os.path.join(tempfile.gettempdir(), "v3stage.txt")
    try:
        with open(stage, "a", encoding="utf-8") as fh:
            fh.write("%s main-start\n" % time.time())
    except Exception:
        pass
    parser = argparse.ArgumentParser(description="V3 本地 Agent")
    parser.add_argument("--config", default=None, help="配置文件路径（默认 ./config.json）")
    group = parser.add_mutually_exclusive_group(required=False)
    group.add_argument("--console", action="store_true", help="控制台模式：stdin 每行一个 JSON 请求")
    group.add_argument("--once", default=None, metavar="JSON", help="执行单个请求后退出")
    group.add_argument("--ws", action="store_true", help="WebSocket 模式连接云端中转")
    group.add_argument("--http", action="store_true", help="HTTP 轮询模式（默认走配置 transport）")
    args = parser.parse_args()

    cfg = cfgmod.load(args.config)
    # 源码模式也要有装机指纹，否则用 `python agent_gui.py`/`-m v3agent.main` 起的
    # Agent 在云端看来永远是"无指纹的老客户端"，同名准入对它形同不存在
    cfg["agent"]["install_id"] = cfgmod.ensure_install_id(
        cfg, (cfg.get("_meta") or {}).get("config_path"))
    try:
        with open(stage, "a", encoding="utf-8") as fh:
            fh.write("%s cfg-ok\n" % time.time())
    except Exception:
        pass
    _setup_logging(cfg)
    try:
        with open(stage, "a", encoding="utf-8") as fh:
            fh.write("%s log-ok\n" % time.time())
    except Exception:
        pass
    agent = LocalAgent(cfg)
    try:
        with open(stage, "a", encoding="utf-8") as fh:
            fh.write("%s agent-ok\n" % time.time())
    except Exception:
        pass

    if args.http or (cfg["agent"].get("transport", "ws") == "http" and not (args.ws or args.console or args.once)):
        base = str(cfg["agent"].get("relay_url", "")).replace("ws://", "http://").replace("wss://", "https://")
        for tail in ("/ws/agent", "/agent"):
            if base.endswith(tail):
                base = base[: -len(tail)]
                break
        run_http(agent, base, cfg["agent"]["name"], cfg["agent"]["secret"])
    elif args.ws or (not args.console and not args.once):
        a = cfg["agent"]
        try:
            with open(stage, "a", encoding="utf-8") as fh:
                fh.write("%s pre-ws\n" % time.time())
        except Exception:
            pass
        run_ws(agent, a["relay_url"], a["name"], a["secret"], a["reconnect_sec"], a["heartbeat_sec"])
    elif args.once:
        req = json.loads(args.once)
        result = agent.handle_request(req)
        try:
            print(json.dumps(result, ensure_ascii=False))
        except UnicodeEncodeError:
            print(json.dumps(result, ensure_ascii=True))
    else:
        run_console(agent)


if __name__ == "__main__":
    main()
