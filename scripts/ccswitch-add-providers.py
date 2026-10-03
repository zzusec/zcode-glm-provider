#!/usr/bin/env python3
"""把 zcode-glm 本地供应商写入 cc-switch.db（claude + codex），幂等。

- 已存在同名供应商则跳过
- codex 的 config.toml 模板取自当前使用中的 codex 供应商（保留 projects/hooks 信任段），
  仅替换头部（model/key/上下文）与 base_url；模板不可用时退化为最小可用配置
- 写入前自动备份 cc-switch.db
"""
import json
import shutil
import sqlite3
import time
import uuid
from pathlib import Path

DB = Path.home() / ".cc-switch" / "cc-switch.db"
BASE = "http://127.0.0.1:15730"
NAME = "zcode-glm 本地"
MODEL = "GLM-5.3-Flash"


def atomic_backup(db):
    bak = Path(str(DB) + ".bak_zcode_glm_" + time.strftime("%Y%m%d_%H%M%S"))
    shutil.copy2(db, bak)
    print("已备份:", bak)


def provider_exists(db, app_type):
    n = db.execute("SELECT COUNT(*) FROM providers WHERE app_type=? AND name=?",
                   (app_type, NAME)).fetchone()[0]
    return n > 0


def insert(db, app_type, settings_config, meta, url):
    pid = str(uuid.uuid4())
    db.execute(
        "INSERT INTO providers (id, app_type, name, settings_config, website_url, category, "
        "created_at, sort_index, notes, icon, icon_color, meta, is_current, in_failover_queue, "
        "cost_multiplier, limit_daily_usd, limit_monthly_usd, provider_type) "
        "VALUES (?, ?, ?, ?, NULL, NULL, ?, NULL, NULL, NULL, NULL, ?, 0, 0, '1.0', NULL, NULL, NULL)",
        (pid, app_type, NAME, json.dumps(settings_config, ensure_ascii=False),
         int(time.time() * 1000), json.dumps(meta)))
    db.execute("INSERT INTO provider_endpoints (provider_id, app_type, url, added_at) "
               "VALUES (?, ?, ?, ?)", (pid, app_type, url, int(time.time() * 1000)))
    return pid


def claude_config():
    env = {
        "ANTHROPIC_AUTH_TOKEN": "zcode-local",
        "ANTHROPIC_BASE_URL": BASE,
        "ANTHROPIC_MODEL": MODEL,
        "CLAUDE_CODE_MAX_CONTEXT_TOKENS": "200000",
        "CLAUDE_CODE_SUBAGENT_MODEL": MODEL,
    }
    for tier in ("FABLE", "HAIKU", "OPUS", "SONNET"):
        env[f"ANTHROPIC_DEFAULT_{tier}_MODEL"] = MODEL
        env[f"ANTHROPIC_DEFAULT_{tier}_MODEL_NAME"] = MODEL
    return ({"env": env, "model": MODEL},
            {"commonConfigEnabled": False, "endpointAutoSelect": True, "apiFormat": "anthropic"},
            BASE)


def codex_config(db):
    row = db.execute(
        "SELECT settings_config FROM providers WHERE app_type='codex' AND is_current=1"
    ).fetchone() or db.execute(
        "SELECT settings_config FROM providers WHERE app_type='codex' LIMIT 1").fetchone()
    catalog = {"models": [{"model": MODEL, "displayName": MODEL,
                           "contextWindow": 200000, "reasoningLevels": ["high"]}]}
    meta = {"commonConfigEnabled": False, "endpointAutoSelect": True,
            "apiFormat": "openai_responses"}
    head_new = (f'model = "{MODEL}"\n'
                'openai_api_key = "zcode-local"\n'
                'model_context_window = 200000\n'
                'model_auto_compact_token_limit = 160000\n')
    tail_new = (f'[model_providers.cc-switch]\nname = "Custom"\n'
                f'base_url = "{BASE}/v1"\nwire_api = "responses"\n'
                'requires_openai_auth = false')
    if row:
        cfg = json.loads(row[0]).get("config", "")
        try:
            i = cfg.index('model = "')
            j = cfg.index("\n\n", i)
            cfg = head_new + cfg[j + 2:]
            import re
            cfg = re.sub(r'base_url = "[^"]*"', f'base_url = "{BASE}/v1"', cfg)
            k = cfg.index("[model_providers.")
            cfg = cfg[:k] + tail_new
            return ({"auth": {"OPENAI_API_KEY": "zcode-local"}, "config": cfg,
                     "modelCatalog": catalog}, meta, BASE + "/v1")
        except (ValueError, IndexError):
            print("模板解析失败，退化为最小配置")
    minimal = (head_new + '\nwire_api = "responses"\ndisable_response_storage = true\n'
               'model_provider = "cc-switch"\n\n' + tail_new)
    return ({"auth": {"OPENAI_API_KEY": "zcode-local"}, "config": minimal,
             "modelCatalog": catalog}, meta, BASE + "/v1")


def main():
    if not DB.exists():
        raise SystemExit("未找到 cc-switch.db")
    atomic_backup(DB)
    db = sqlite3.connect(str(DB), timeout=15)
    db.execute("PRAGMA busy_timeout=15000")
    for app_type, builder in (("claude", claude_config), ("codex", codex_config)):
        if provider_exists(db, app_type):
            print(f"{app_type}: 已存在「{NAME}」，跳过")
            continue
        sc, meta, url = builder(db)
        pid = insert(db, app_type, sc, meta, url)
        print(f"{app_type}: 已写入（id={pid}）")
    db.commit()
    db.close()
    print("完成。请在 CC Switch 中切换供应商（或重启 CC Switch 刷新列表）。")


if __name__ == "__main__":
    main()
