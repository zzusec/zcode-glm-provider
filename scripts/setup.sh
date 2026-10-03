#!/usr/bin/env bash
# zcode-glm 本地反代：一键安装 / 修复（幂等）
# 用法: bash setup.sh [--skip-smoke]
set -euo pipefail

SKILL_DIR="$(cd "$(dirname "$0")/.." && pwd)"
INSTALL_DIR="$HOME/.zcode-glm-provider"
PLIST_LABEL="com.hx10.zcode-glm-openai-proxy"
PLIST="$HOME/Library/LaunchAgents/${PLIST_LABEL}.plist"
PORT=15730
SMOKE=1
[[ "${1:-}" == "--skip-smoke" ]] && SMOKE=0

echo "== 1/6 前置检查 =="
[[ -x /opt/homebrew/bin/zcode ]] || { echo "未找到 zcode CLI"; exit 1; }
[[ -f "$HOME/.zcode/v2/config.json" ]] || { echo "缺少 ~/.zcode/v2/config.json（需在 ZCode 桌面端登录 plan）"; exit 1; }
/usr/bin/python3 -c "import aiohttp" 2>/dev/null || { echo "/usr/bin/python3 缺少 aiohttp"; exit 1; }
echo "OK"

echo "== 2/6 v2/provider_config.json start-plan 规则 =="
python3 "$SKILL_DIR/scripts/ensure-provider-rule.py"

echo "== 3/6 部署代理与 LaunchAgent =="
mkdir -p "$INSTALL_DIR"
cp "$SKILL_DIR/scripts/zcode-glm-openai-proxy.py" "$INSTALL_DIR/"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>${PLIST_LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>${INSTALL_DIR}/zcode-glm-openai-proxy.py</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>${INSTALL_DIR}/proxy.log</string>
  <key>StandardErrorPath</key><string>${INSTALL_DIR}/proxy.log</string>
</dict>
</plist>
EOF
launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"
sleep 2
echo "OK"

echo "== 4/6 环境修复检查（clash 系统代理会拒绝 loopback）=="
if ! grep -qs "no_proxy" "$HOME/.zshenv"; then
  cat >> "$HOME/.zshenv" <<'EOF'

# 本机代理服务（claude/codex 本地反代等）不走系统代理；clash 会拒绝 loopback 目标
export no_proxy="127.0.0.1,localhost,::1,*.local"
export NO_PROXY="$no_proxy"
EOF
  echo "已向 ~/.zshenv 追加 no_proxy"
else
  echo "~/.zshenv 已含 no_proxy"
fi
# macOS 例外列表若被写成单条逗号串（clash 客户端的 bug），修复为标准格式
first=$(networksetup -getproxybypassdomains "Wi-Fi" 2>/dev/null | head -1 || true)
if [[ "$first" == *,* ]]; then
  echo "检测到 Wi-Fi 代理例外列表为坏的逗号拼接格式，正在修复..."
  IFS=',' read -ra items <<< "$first"
  networksetup -setproxybypassdomains "Wi-Fi" "${items[@]}"
  echo "已修复"
else
  echo "系统代理例外列表格式正常"
fi

echo "== 5/6 cc-switch 供应商写入 =="
python3 "$SKILL_DIR/scripts/ccswitch-add-providers.py"

echo "== 6/6 健康检查 =="
curl -sf -m 5 "http://127.0.0.1:${PORT}/health" && echo
if [[ $SMOKE -eq 1 ]]; then
  echo "发一次真实对话冒烟测试（消耗约 3.5k token）..."
  curl -s -m 120 -X POST "http://127.0.0.1:${PORT}/v1/chat/completions" \
    -H "Content-Type: application/json" \
    -d '{"messages":[{"role":"user","content":"Reply with exactly: ZGLM_SETUP_OK"}]}' \
    | python3 -c "import json,sys; d=json.load(sys.stdin); print('smoke:', d['choices'][0]['message']['content'])"
fi
echo "全部完成。接口: http://127.0.0.1:${PORT}/v1/chat/completions | /v1/messages | /v1/responses"
