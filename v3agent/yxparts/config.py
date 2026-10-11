# -*- coding: utf-8 -*-
"""连接配置的读写。"""
from __future__ import annotations

import json
from pathlib import Path


DEFAULT_CONFIG = {
    "sqlserver": {
        "server": "",
        "port": 1433,
        "database": "",
        "user": "",
        "password": "",
        "windows_auth": True,
        "tds_version": "7.0",
        "table": "HPKhType",
        "field_match": "hpmckh",
        "field_spec": "GgxhKh",
        "field_spec2": "HpbhKh",
        "field_code": "hpbh",
        "field_unit": "HpUnitName",
    },
    "hpdan": {
        "table": "hpdan",
        "name_field": "hpmc",
        "code_field": "hpbh",
        "unit_field": "hpunit",
        "spec_field": "Ggxh",
        "lbtype_kh": "21-",
    },
}


def load_config(path: str | Path) -> dict:
    path = Path(path)
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            for section, values in data.items():
                if section in cfg and isinstance(values, dict):
                    cfg[section].update(values)
                else:
                    cfg[section] = values
        except Exception:
            pass
    return cfg


def save_config(path: str | Path, cfg: dict):
    Path(path).write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
