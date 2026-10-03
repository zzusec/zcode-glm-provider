---
name: zcode-glm-provider
description: 把本机 ZCode 的 GLM（bigmodel start-plan）反代成本地 OpenAI/Anthropic 双协议 HTTP 接口（127.0.0.1:15730），并一键写入 cc-switch 供 claude/codex 使用。当用户提到 zcode 反代、本地 GLM 接口、zcode-glm、端口 15730、cc-switch 添加本地模型供应商、claude/codex 连本地模型，或 zcode 反代挂了/502/要修复重装时使用本 skill——即使用户没有明说"反代"两个字。
---

# ZCode GLM 本地反代

把本机 ZCode 桌面端在用的 GLM-5.3-Flash（start-plan 额度）通过 zcode CLI 无头模式反代为 HTTP 接口，并提供 claude（Anthropic 协议）和 codex（OpenAI Responses 协议）两套端点。

## 接口

| 端点 | 协议 | 用途 |
|---|---|---|
| `POST /v1/chat/completions` | OpenAI Chat | 通用 |
| `POST /v1/responses` | OpenAI Responses | codex（≥0.160 仅支持此协议） |
| `POST /v1/messages` | Anthropic | claude / Claude Code |
| `GET /v1/models`、`GET /health` | - | 模型列表 / 健康检查 |

模型固定 `GLM-5.3-Flash`；多轮消息自动拍平为带历史的单轮 prompt；流式为"完成后分块"伪流式（CLI 本身无真流式）。

## 安装 / 修复（一条命令，幂等）

```bash
bash ~/.agents/skills/zcode-glm-provider/scripts/setup.sh
```

依次执行：前置检查 → 修复 `~/.zcode/v2/provider_config.json` 的 start-plan 规则（首条 + 刷新签名凭据/JWT）→ 部署代理与 LaunchAgent（`~/Library/LaunchAgents/com.hx10.zcode-glm-openai-proxy.plist`，日志 `~/.zcode-glm-provider/proxy.log`）→ 检查 no_proxy 与系统代理例外列表 → 幂等写入 cc-switch 的 claude+codex 供应商（自动备份库）→ 健康检查与冒烟测试。

只修 cc-switch 配置（不动代理）：`python3 ~/.agents/skills/zcode-glm-provider/scripts/ccswitch-add-providers.py`
只修 zcode 供应商规则：`python3 ~/.agents/skills/zcode-glm-provider/scripts/ensure-provider-rule.py`

## 前置条件

- ZCode 桌面端已登录且会话模型为 start-plan 的 GLM（JWT 存于 `~/.zcode/v2/config.json`）
- `/opt/homebrew/bin/zcode` CLI 存在；系统 python3 有 aiohttp

## 排障

遇到 502 / 1113 余额不足 / 签名错误 / codex 连不上本地端口等问题，先读
`references/troubleshooting.md`（含请求链路、签名凭据解密方法、clash 系统代理坑、cc-switch 库表结构速查），
多数情况重跑 `setup.sh` 即可恢复。

## 验证

```bash
curl -s http://127.0.0.1:15730/health
curl -s -X POST http://127.0.0.1:15730/v1/chat/completions -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"hi"}]}'
```

cc-switch 配置生效需在 UI 里切换到「zcode-glm 本地」供应商（或重启 CC Switch 刷新列表）。
