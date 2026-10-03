# zcode-glm-provider

把本机 [ZCode](https://zcode.z.ai) 桌面端正在使用的 GLM（bigmodel start-plan，`GLM-5.3-Flash`）反代为本地 HTTP 接口，并提供 claude（Anthropic 协议）与 codex（OpenAI Responses 协议）两套端点，一键写入 [CC Switch](https://github.com/farion1231/cc-switch) 供 Claude Code / Codex CLI 切换使用。

> 这是一个 ZCode skill。安装后对 agent 说「zcode 反代挂了修一下」即可自动排障。

## 接口

| 端点 | 协议 | 用途 |
|---|---|---|
| `POST /v1/chat/completions` | OpenAI Chat | 通用 OpenAI 客户端 |
| `POST /v1/responses` | OpenAI Responses | Codex CLI（≥0.160 仅支持此协议） |
| `POST /v1/messages` | Anthropic | Claude Code |
| `GET /v1/models` · `GET /health` | - | 模型列表 / 健康检查 |

- 模型：`GLM-5.3-Flash`（走你自己的 start-plan 额度）
- 多轮消息自动拍平为带历史上下文的单轮 prompt
- 流式接口为「完成后分块」伪流式（zcode CLI 无真流式输出）
- 所有工具调用被禁用，仅做纯文本对话，防止 API 调用方提示注入触发本机命令执行
- 仅监听 `127.0.0.1:15730`，凭据全部留在本机

## 安装

前置条件：macOS、ZCode 桌面端已登录 start-plan、[CC Switch](https://github.com/farion1231/cc-switch)（可选）、系统 python3 带 aiohttp。

```bash
git clone https://github.com/zzusec/zcode-glm-provider.git ~/.agents/skills/zcode-glm-provider
bash ~/.agents/skills/zcode-glm-provider/scripts/setup.sh
```

`setup.sh` 幂等，依次完成：

1. 前置检查（zcode CLI、aiohttp、plan 登录态）
2. 修复/刷新 `~/.zcode/v2/provider_config.json` 首条供应商规则（zcode 无头模式固定读它；自动解密签名凭据 + 刷新 JWT，重新登录后也能自愈）
3. 部署反代 `~/.zcode-glm-provider/` 并注册 LaunchAgent（`com.hx10.zcode-glm-openai-proxy`，开机自启 + 崩溃拉起）
4. 环境修复：`~/.zshenv` 追加 `no_proxy`、修复 clash 写坏的 macOS 系统代理例外列表
5. 幂等写入 CC Switch 的 claude + codex 供应商「zcode-glm 本地」（写库前自动备份）
6. 健康检查 + 冒烟测试

然后在 CC Switch 里切换到「zcode-glm 本地」即可。

## 快速验证

```bash
curl -s http://127.0.0.1:15730/health
curl -s -X POST http://127.0.0.1:15730/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"你好"}]}'
```

## 已知限制

- 每次请求约 3.5k 固定输入 token（zcode 系统提示开销），延迟 3~8 秒，适合日常低频使用
- 依赖 `~/.zcode/v2/provider_config.json` 首条规则；被其他工具重写后重跑 `scripts/ensure-provider-rule.py`
- codex 会读 macOS 系统代理且不认例外列表，clash 开启系统代理时 loopback 会被拒——`setup.sh` 已通过 `~/.zshenv` 的 `no_proxy` 规避；clash 重新开关系统代理可能再次写坏例外列表（重跑 `setup.sh` 自动修复）

排障细节（请求链路、签名凭据解密、cc-switch 库表结构）见 [`references/troubleshooting.md`](references/troubleshooting.md)。

## 声明

仅供个人在本地使用自己已登录账号的额度，请遵守 Z.AI / ZCode 服务条款；与 Z.ai 官方无关。

## License

MIT
