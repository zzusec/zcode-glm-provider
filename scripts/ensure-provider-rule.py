#!/usr/bin/env python3
"""确保 ~/.zcode/v2/provider_config.json 的首条 rule 指向 start-plan 网关。

zcode 无头模式（`zcode -p`）以该文件的第一条 providerRule 作为默认模型通道。
本脚本幂等：
- rule 已存在 → 刷新签名凭据与 JWT（两者都会随重新登录轮换），并挪到首位
- rule 不存在 → 从 v2/config.json 取 JWT、从 credentials.json 解密签名 key 后插入
"""
import base64
import json
import os
import subprocess
import sys
from pathlib import Path

HOME = Path.home()
ZCODE = HOME / ".zcode"
V2 = ZCODE / "v2"
PROVIDER_ID = "bigmodel-start-plan"
GATEWAY = "https://zcode.z.ai/api/v1/zcode-plan/anthropic"
MODEL_IDS = ["GLM-5.3-Flash", "GLM-5.3"]


def atomic_write_json(path, data):
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    os.replace(tmp, path)


def jwt_uid(jwt):
    payload = jwt.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload)).get("user_id")


def decrypt_credential(enc_value):
    """解密 credentials.json 的 enc:v1 值（aes-256-gcm，密钥为本机标准派生）。"""
    script = r'''
const crypto = require("crypto");
const os = require("os");
const secret = process.env.ZCODE_CREDENTIAL_SECRET?.trim() ||
  `zcode-credential-fallback:${os.platform()}:${os.homedir()}:${os.userInfo().username}`;
const key = crypto.createHash("sha256").update(secret).digest();
const val = process.argv[1];
if (!val.startsWith("enc:v1:")) { console.log(val); process.exit(0); }
const [ivB64, tagB64, ctB64] = val.slice("enc:v1:".length).split(".");
const d = crypto.createDecipheriv("aes-256-gcm", key,
  Buffer.from(ivB64, "base64url"));
d.setAuthTag(Buffer.from(tagB64, "base64url"));
process.stdout.write(Buffer.concat([d.update(Buffer.from(ctB64, "base64url")), d.final()]).toString());
'''
    out = subprocess.run(["node", "-e", script, enc_value],
                         capture_output=True, text=True, check=True)
    return out.stdout.strip()


def main():
    cfg = json.load(open(V2 / "config.json"))
    start_plan = cfg["provider"].get("builtin:bigmodel-start-plan")
    if not start_plan:
        sys.exit("v2/config.json 中没有 builtin:bigmodel-start-plan（需要先在桌面端登录 plan）")
    jwt = start_plan["options"]["apiKey"]
    uid = jwt_uid(jwt)

    creds = json.load(open(V2 / "credentials.json"))
    enc_key = creds.get(
        f"account-provider:coding-plan:account:bigmodel-individual-coding-plan:account:{uid}:api-key")
    signing_key = decrypt_credential(enc_key) if enc_key else None
    if not signing_key or signing_key.count(".") != 1:
        sys.exit(f"未能解密 uid={uid} 的签名凭据（credentials.json 缺失或格式变化）")

    pc_path = V2 / "provider_config.json"
    pc = json.load(open(pc_path))
    rules = pc["config"]["providerConfigRules"]["providerRules"]
    rule = next((r for r in rules if r.get("providerId") == PROVIDER_ID), None)
    if rule is None:
        rule = {
            "providerId": PROVIDER_ID,
            "providerName": "BigModel Start Plan",
            "config": {
                "group": "standard-personal",
                "access": {"type": "api-key", "apiKey": signing_key},
                "api": {"type": "anthropic-messages", "baseUrl": GATEWAY,
                        "headers": {"x-api-key": jwt}},
                "personalModelIds": MODEL_IDS,
                "modelOrder": MODEL_IDS,
            },
        }
        rules.insert(0, rule)
        print("已插入 start-plan rule（首位）")
    else:
        changed = False
        if rule["config"]["access"].get("apiKey") != signing_key:
            rule["config"]["access"]["apiKey"] = signing_key
            changed = True
        if rule["config"].get("api", {}).get("headers", {}).get("x-api-key") != jwt:
            rule["config"].setdefault("api", {})["baseUrl"] = GATEWAY
            rule["config"]["api"].setdefault("headers", {})["x-api-key"] = jwt
            changed = True
        if rules[0] is not rule:
            rules.remove(rule)
            rules.insert(0, rule)
            changed = True
        print("rule 已刷新" if changed else "rule 已是最新")

    atomic_write_json(pc_path, pc)


if __name__ == "__main__":
    main()
