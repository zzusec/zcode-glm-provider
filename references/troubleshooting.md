# 故障排查与原理速查

## 请求链路

```
claude/codex → 127.0.0.1:15730 (本反代)
  → zcode -p <拍平的prompt> --json   (无头单轮，走 zcode 自己的网络栈与签名)
    → ~/.zcode/v2/provider_config.json 第一条 providerRule
      → https://zcode.z.ai/api/v1/zcode-plan/anthropic (start-plan 网关, Anthropic 协议)
```

## 常见故障

### 1. 代理 502：`zcode 失败(rc=1)` 且错误含 `1113 余额不足`
`-p` 模式固定用 `~/.zcode/v2/provider_config.json` 的**第一条** providerRule。
首条不是 `bigmodel-start-plan`（被其他工具/客户端重写）就会落到无余额的 key 上。
→ 重跑 `scripts/ensure-provider-rule.py`。

### 2. 代理 502：`ClientRequestSigningV4Error: must contain one separator`
plan 网关要求请求签名，签名凭据必须是 `id.secret`（恰好一个点）的 coding-plan key，
而不是 JWT。JWT 放在 rule 的 `api.headers.x-api-key` 里做认证。
两者都会随重新登录轮换 → 重跑 `scripts/ensure-provider-rule.py`（会自动刷新）。

签名凭据的解密来源：`~/.zcode/v2/credentials.json` 里
`account-provider:coding-plan:account:bigmodel-individual-coding-plan:account:<uid>:api-key`，
`enc:v1` = aes-256-gcm，key = sha256("zcode-credential-fallback:{platform}:{homedir}:{username}")，
布局 `enc:v1:<iv b64url>:<tag b64url>:<ct b64url>`。若设置了 `ZCODE_CREDENTIAL_SECRET` 环境变量则改用它。

### 3. codex 报 `502 Bad Gateway` 但反代日志无请求
codex 走了 clash 系统代理，clash 拒绝一切 loopback/内网目标。两层修复：
- `~/.zshenv` 里 `export no_proxy="127.0.0.1,localhost,::1,*.local"`（codex 只认这个，不认 macOS 例外列表）
- macOS 例外列表若被 clash 写成单条逗号拼接字符串（`networksetup -getproxybypassdomains "Wi-Fi"` 首行含逗号即为坏），
  用 `networksetup -setproxybypassdomains "Wi-Fi" <逐条...>` 修复，否则其他读系统代理的应用也会坏。
- 注意：clash 客户端重新开关"系统代理"可能再次写坏例外列表。

### 4. codex 报 `wire_api = "chat" is no longer supported`
codex ≥ 0.160 只支持 responses 协议，反代已实现 `/v1/responses`，配置里保持 `wire_api = "responses"`。

### 5. claude 报 `[claude-code:unrecognized_model]`
无害警告。供应商 env 里已有 `CLAUDE_CODE_MAX_CONTEXT_TOKENS=200000` 声明真实窗口。

### 6. LaunchAgent 下 zcode 报 `env: node: No such file or directory`
GUI 环境没有 homebrew PATH。反代已内置 PATH 注入（`/opt/homebrew/bin:...`），勿删。

## cc-switch 数据库要点（~/.cc-switch/cc-switch.db）

- 表 `providers`：主键 (id, app_type)。`settings_config` 因 app_type 而异：
  - claude: `{"env": {ANTHROPIC_AUTH_TOKEN/ANTHROPIC_BASE_URL/各档位模型映射}, "model": "..."}`
  - codex: `{"auth": {"OPENAI_API_KEY": ...}, "config": "<完整 config.toml 字符串>", "modelCatalog": {"models": [{model, displayName, contextWindow, reasoningLevels}]}}`
  - codex 切换供应商时 cc-switch 用 modelCatalog 生成 `~/.codex/cc-switch-model-catalog.json`
- 表 `provider_endpoints`：每个供应商的 endpoint URL 记录（endpointAutoSelect 用）
- 直接改库后需重启 CC Switch.app 才能在 UI 看到变更；改库前必须备份
- codex 的 config.toml 里保留 `[projects]`/`[hooks.state]` 段可继承项目信任与 hooks 授权

## 成本与限制

- 每次请求约 3.5k 固定输入 token（zcode 系统提示开销），走 start-plan 额度
- 延迟 3~8s/请求；CLI 输出为结束后一次性给出 → 流式接口是"完成后分块"的伪流式
- 工具全部禁用（防 API 调用方提示注入触发本机命令执行），只做纯文本对话
- 并发上限 2、单请求超时 300s（代理内建）
