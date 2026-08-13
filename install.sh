#!/usr/bin/env bash
# =============================================================================
# metano 一键安装脚本 — clone → ./install.sh → 起来
# -----------------------------------------------------------------------------
# 流程：
#   1. 环境检测 (python3≥3.10 / node≥18 / npm)
#   2. Python venv + 依赖 (requirements.txt，venv 位于 $METANO_HOME/.venv)
#   3. 前端构建 (web → npm install && npm run build → web/dist)
#   4. 生成配置 (gen_config.py → gateway_config.yaml)
#   5. 初始化数据库 (bridge.db / evo.db / memory.db)
#   6. 检测 LLM key（缺则提示，并可选运行配置向导 --wizard，不阻塞）
#   7. 启动服务 (metano.sh start) + 健康检查 (healthcheck.sh)
#   8. 完成输出（访问地址 / 初始 admin 密码 / 下一步）
#
# 幂等：可重复执行；已创建的 venv / 已生成的配置 / 已构建的前端安全跳过。
# =============================================================================
set -euo pipefail

# ── 0. 参数解析 ──────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

WITH_EMBEDDING=0
WITH_BROWSER=0
SKIP_FRONTEND=0
SKIP_START=0

usage() {
  cat <<'EOF'
用法: ./install.sh [选项]

选项:
  --with-embedding    额外安装本地向量嵌入 (sentence-transformers + torch，体积大)
  --with-browser      额外安装 Playwright 浏览器自动化 (并下载 chromium)
  --skip-frontend     跳过前端 npm 构建（已构建过 / 无网络时使用）
  --skip-start        只安装，不启动服务（之后可运行 bash metano.sh start）
  --help, -h          显示本帮助

环境变量:
  METANO_HOME         运行时数据目录（DB/配置/备份/venv），默认 ~/.claude/metano
                      (例子: METANO_HOME=/srv/metano ./install.sh)
EOF
}

for arg in "$@"; do
  case "$arg" in
    --with-embedding) WITH_EMBEDDING=1 ;;
    --with-browser)   WITH_BROWSER=1 ;;
    --skip-frontend)  SKIP_FRONTEND=1 ;;
    --skip-start)     SKIP_START=1 ;;
    --help|-h) usage; exit 0 ;;
    *) echo "错误: 未知参数 '$arg'（用 ./install.sh --help 查看用法）" >&2; exit 1 ;;
  esac
done

# ── 路径：METANO_HOME（支持 ~/ 与相对路径）───────────────────────────────────
METANO_HOME="${METANO_HOME:-$HOME/.claude/metano}"
case "$METANO_HOME" in
  "~/"*) METANO_HOME="$HOME/${METANO_HOME#\~/}" ;;
  /*)   : ;;
  *)    METANO_HOME="$(pwd)/$METANO_HOME" ;;
esac
export METANO_HOME

VENV_DIR="$METANO_HOME/.venv"
PYTHON_BIN="$VENV_DIR/bin/python"
PIP_BIN="$VENV_DIR/bin/pip"

# ── 工具函数 ─────────────────────────────────────────────────────────────────
info() { printf '\033[1;34m[metano]\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m[metano]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[metano]\033[0m %s\n' "$*"; }
fail() { printf '\033[1;31m[metano] 错误:\033[0m %s\n' "$*" >&2; exit 1; }

echo ""
info "metano 安装开始（数据目录: $METANO_HOME）"
echo ""

# ════════════════════════════════════════════════════════════════════════════
# 1. 环境检测
# ════════════════════════════════════════════════════════════════════════════
info "[1/8] 检测环境"
command -v python3 >/dev/null 2>&1 || fail "未找到 python3。请安装 Python ≥3.10（Debian/Ubuntu: apt install python3 python3-venv；macOS: brew install python；Windows: python.org）"
PY_VER="$(python3 -c 'import sys; print(".".join(map(str, sys.version_info[:2])))')"
PY_MAJOR="${PY_VER%%.*}"
PY_MINOR="${PY_VER#*.}"
if [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 10 ]; }; then
  fail "需要 Python ≥3.10，当前为 $PY_VER。请升级 Python"
fi
ok "python3: $(python3 --version)"

command -v node >/dev/null 2>&1 || fail "未找到 node。请安装 Node.js ≥18（https://nodejs.org 或 nvm / apt / brew）"
NODE_MAJOR="$(node -p 'process.versions.node.split(".")[0]' 2>/dev/null || echo 0)"
if [ "${NODE_MAJOR:-0}" -lt 18 ]; then
  fail "需要 Node.js ≥18，当前为 $(node --version 2>/dev/null || echo 未知)"
fi
ok "node: $(node --version 2>/dev/null)"

command -v npm >/dev/null 2>&1 || fail "未找到 npm（node 自带，请检查安装是否完整）"
ok "npm: $(npm --version 2>/dev/null)"

command -v curl >/dev/null 2>&1 || warn "未找到 curl（健康检查将受限，建议安装）"
command -v sqlite3 >/dev/null 2>&1 || warn "未找到 sqlite3 CLI（backup.sh 的数据库备份将不可用，不影响核心运行）"
command -v ccc >/dev/null 2>&1 || info "未检测到 ccc (CocoIndex)，离线向量索引为可选功能"

# ════════════════════════════════════════════════════════════════════════════
# 2. Python venv + 依赖
# ════════════════════════════════════════════════════════════════════════════
info "[2/8] Python 虚拟环境与依赖"
mkdir -p "$METANO_HOME"
if [ -x "$PYTHON_BIN" ]; then
  ok "虚拟环境已存在: $VENV_DIR（跳过创建）"
else
  info "创建虚拟环境: $VENV_DIR"
  python3 -m venv "$VENV_DIR" || fail "创建 venv 失败（缺少 python3-venv？Debian/Ubuntu: apt install python3-venv）"
fi

info "安装 Python 依赖 (requirements.txt)..."
"$PIP_BIN" install --upgrade pip >/dev/null 2>&1 || true
"$PIP_BIN" install -r "$SCRIPT_DIR/requirements.txt" || fail "pip install requirements.txt 失败（网络问题? 代理见 .env.example）"

if [ "$WITH_EMBEDDING" = "1" ]; then
  info "安装本地向量嵌入依赖 (requirements-embedding.txt，含 torch，较大)..."
  [ -f "$SCRIPT_DIR/requirements-embedding.txt" ] || fail "缺少 requirements-embedding.txt"
  "$PIP_BIN" install -r "$SCRIPT_DIR/requirements-embedding.txt" || fail "pip install requirements-embedding.txt 失败"
  ok "embedding 依赖已安装；建议 export METANO_EMBED_PYTHON=$PYTHON_BIN"
fi
if [ "$WITH_BROWSER" = "1" ]; then
  info "安装 Playwright 浏览器自动化..."
  "$PIP_BIN" install playwright || fail "pip install playwright 失败"
  "$VENV_DIR/bin/playwright" install chromium || warn "Playwright chromium 下载失败，可稍后手动运行: $VENV_DIR/bin/playwright install chromium"
fi
ok "Python 依赖就绪"

# ════════════════════════════════════════════════════════════════════════════
# 3. 前端构建
# ════════════════════════════════════════════════════════════════════════════
info "[3/8] 前端构建"
if [ "$SKIP_FRONTEND" = "1" ]; then
  warn "跳过前端构建 (--skip-frontend)"
elif [ -f "$SCRIPT_DIR/web/package.json" ]; then
  if [ ! -d "$SCRIPT_DIR/web/node_modules" ]; then
    info "npm install（首次，可能需要几分钟）..."
  else
    info "npm install（增量）..."
  fi
  ( cd "$SCRIPT_DIR/web" && npm install && npm run build ) || fail "前端构建失败（网络问题? 重试或用 --skip-frontend 跳过；依赖见 web/package.json）"
  ok "前端已构建 → $SCRIPT_DIR/web/dist"
else
  warn "未找到 web/package.json，跳过前端构建"
fi

# ════════════════════════════════════════════════════════════════════════════
# 4. 生成配置
# ════════════════════════════════════════════════════════════════════════════
info "[4/8] 生成配置 (gen_config.py)"
GEN_CONFIG="$SCRIPT_DIR/gen_config.py"
CONFIG_FILE="$METANO_HOME/gateway_config.yaml"

if [ -f "$CONFIG_FILE" ]; then
  ok "配置文件已存在: $CONFIG_FILE（跳过生成）"
else
  if [ ! -f "$GEN_CONFIG" ]; then
    warn "缺少 $GEN_CONFIG（gen_config.py 由项目提供，负责生成 gateway_config.yaml 与初始管理员）。"
    warn "继续安装：首次启动 Web 时将自动生成默认配置与随机 admin 密码。"
  else
    info "运行 $GEN_CONFIG → $CONFIG_FILE ..."
    ( cd "$SCRIPT_DIR" && "$PYTHON_BIN" "$GEN_CONFIG" ) || fail "gen_config.py 生成配置失败（请检查其输出 / 依赖）"
    [ -f "$CONFIG_FILE" ] || fail "gen_config.py 执行后未生成 $CONFIG_FILE"
    ok "配置已生成"
  fi
fi

# ════════════════════════════════════════════════════════════════════════════
# 5. 初始化数据库
# ════════════════════════════════════════════════════════════════════════════
info "[5/8] 初始化数据库 (bridge.db / evo.db / memory.db)"
(
  cd "$SCRIPT_DIR"
  # audit: DBs created here must be 0600 — never rely on the shell umask.
  umask 077
  "$PYTHON_BIN" -c "from metano.db import init_db; init_db(); from metano.evo_models import init_db as _i; _i(); from metano.memory import _get_conn;
with _get_conn():
    pass" || fail "数据库初始化失败"
)
for dbname in bridge.db evo.db memory.db; do
  [ -f "$METANO_HOME/$dbname" ] && ok "  ✓ $dbname"
done
ok "数据库就绪"

# ════════════════════════════════════════════════════════════════════════════
# 6. 检测 LLM key（不阻塞安装）
# ════════════════════════════════════════════════════════════════════════════
info "[6/8] 检查 LLM key"
# 安全加载 .env：只用 python 解析键值对（不当作 shell 执行），
# 且先校验属主/权限/符号链接，并拒绝含 shell 元字符的值（M-05）。
if [ -f "$METANO_HOME/.env" ]; then
  info "加载 $METANO_HOME/.env（安全解析，不执行 shell）"
  _env_safe="$("$PYTHON_BIN" - "$METANO_HOME/.env" <<'PYEOF'
import os, stat, sys
path = sys.argv[1]
try:
    st = os.lstat(path)
except OSError as e:
    sys.stderr.write(f"无法读取 .env: {e}\n"); sys.exit(1)
if stat.S_ISLNK(st.st_mode):
    sys.stderr.write(".env 是符号链接，拒绝加载\n"); sys.exit(1)
uid = os.geteuid()
if st.st_uid != uid and st.st_uid != 0:
    sys.stderr.write(f".env 属主 UID {st.st_uid} 与当前用户 {uid} 不符，拒绝加载\n"); sys.exit(1)
if st.st_mode & 0o022:
    sys.stderr.write(".env 权限过宽（group/other 可写），拒绝加载；请 chmod 600\n"); sys.exit(1)
bad = set("$`;|&")
out = []
for lineno, raw in enumerate(open(path, encoding='utf-8'), 1):
    line = raw.rstrip("\r\n")
    if not line.strip() or line.lstrip().startswith("#"):
        continue
    if line.startswith("export "):
        line = line[len("export "):]
    if "=" not in line:
        sys.stderr.write(f".env 第 {lineno} 行缺少 '='，拒绝加载\n"); sys.exit(1)
    key, _, value = line.partition("=")
    key = key.strip()
    value = value.strip()
    if not key or not key.replace("_", "a").isalnum() or key[0].isdigit():
        sys.stderr.write(f".env 第 {lineno} 行变量名非法: {key!r}\n"); sys.exit(1)
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    if any(c in value for c in bad):
        sys.stderr.write(f".env 第 {lineno} 行变量 {key} 含非法字符，拒绝加载\n"); sys.exit(1)
    out.append(f"{key}={value}")
sys.stdout.write("\n".join(out))
PYEOF
)" || fail "安全解析 .env 失败（请检查属主/权限/内容，参考 .env.example）"
  while IFS= read -r _env_line; do
    [ -n "$_env_line" ] || continue
    _env_key="${_env_line%%=*}"
    _env_val="${_env_line#*=}"
    export "$_env_key=$_env_val"
  done <<< "$_env_safe"
  info ".env 已加载"
fi

_is_placeholder() {
  case "${1:-}" in
    ""|your-key|your_api_key|your-api-key|sk-xxx|xxx|changeme|CHANGE_ME|placeholder|your-tavily-key|your-ha-token) return 0 ;;
    *) return 1 ;;
  esac
}

# 同时检查 gateway_config.yaml 中 models.default.api_key（gen_config.py --wizard 写入的位置）
CFG_API_KEY=""
if [ -f "$CONFIG_FILE" ]; then
  CFG_API_KEY="$("$PYTHON_BIN" -c '
import os
try:
    import yaml
    from pathlib import Path
    p = Path(os.environ.get("METANO_HOME", "")) / "gateway_config.yaml"
    if p.exists():
        c = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        print((c.get("models") or {}).get("default", {}).get("api_key", "") or "")
    else:
        print("")
except Exception:
    print("")
' 2>/dev/null || true)"
fi

if _is_placeholder "${ANTHROPIC_API_KEY:-}" && _is_placeholder "$CFG_API_KEY"; then
  warn "未检测到有效的 LLM key。系统仍可启动，但 AI 功能（对话/记忆/进化）需要 key 后才可用。"
  warn "配置方式："
  warn "  ① 交互式向导（推荐）: python3 gen_config.py --wizard"
  warn "  ② 写入 $METANO_HOME/.env 或 export 到 shell:"
  warn "     ANTHROPIC_BASE_URL=https://opencode.ai/zen/go"
  warn "     ANTHROPIC_API_KEY=sk-..."
  warn "     ANTHROPIC_MODEL=claude-sonnet-4-6"
  warn "     HONCHO_MODEL=claude-sonnet-4-6"
  if [ -t 0 ]; then
    echo ""
    read -r -p "[metano] 是否立即运行配置向导 (gen_config.py --wizard)？[Y/n]: " run_wizard_now || run_wizard_now="Y"
  else
    run_wizard_now="n"   # 非交互终端（CI / 管道）不自动进入向导
  fi
  case "${run_wizard_now:-Y}" in
    Y|y|是|1)
      info "运行配置向导 (gen_config.py --wizard) ..."
      ( cd "$SCRIPT_DIR" && "$PYTHON_BIN" "$GEN_CONFIG" --wizard ) || warn "配置向导未完成（可稍后重跑: python3 gen_config.py --wizard）"
      ;;
    *)
      ok "跳过向导，之后可随时运行: python3 gen_config.py --wizard"
      ;;
  esac
else
  ok "已检测到 LLM key（env 或 gateway_config.yaml）"
  [ -n "${ANTHROPIC_BASE_URL:-}" ] && ok "ANTHROPIC_BASE_URL: $ANTHROPIC_BASE_URL"
  if _is_placeholder "${TAVILY_API_KEY:-}"; then
    warn "提示: 未配置 TAVILY_API_KEY，搜索将使用 duckduckgo 引擎"
  fi
fi

# ════════════════════════════════════════════════════════════════════════════
# 7. 启动服务 + 健康检查
# ════════════════════════════════════════════════════════════════════════════
info "[7/8] 启动服务"
if [ "$SKIP_START" = "1" ]; then
  warn "跳过启动 (--skip-start)。之后可运行: bash metano.sh start"
else
  # 让 metano.sh 内的 python3 优先使用 venv；METANO_DIR 兼容 backup/healthcheck
  export PATH="$VENV_DIR/bin:$PATH"
  export METANO_DIR="$METANO_HOME"
  export HF_HUB_OFFLINE="${HF_HUB_OFFLINE:-1}"
  # metano.sh 启动服务时会 cd 到 $METANO_HOME 再 import metano 包；
  # 代码 clone 在任意目录时，必须让 python 能找到 repo 里的 metano 包
  export PYTHONPATH="$SCRIPT_DIR${PYTHONPATH:+:$PYTHONPATH}"

  info "执行 metano.sh start ..."
  if bash "$SCRIPT_DIR/metano.sh" start; then
    ok "metano.sh start 完成"
  else
    warn "全量 start 中断（常见原因: 可选组件 ccc 缺失），改为逐服务补齐..."
    bash "$SCRIPT_DIR/metano.sh" start web || true
    bash "$SCRIPT_DIR/metano.sh" start cron || true
    bash "$SCRIPT_DIR/metano.sh" start gateway || true
  fi

  if ! command -v ccc >/dev/null 2>&1; then
    warn "未检测到 ccc (CocoIndex)，离线向量索引未启动（可选，不影响核心 Web）"
  fi

  info "健康检查 (healthcheck.sh) ..."
  if bash "$SCRIPT_DIR/healthcheck.sh"; then
    ok "全部服务健康"
  else
    warn "部分服务未就绪（见上方报告；cocoindex 需 ccc、网关需对应平台 key，均不影响 Web 使用）"
  fi

  # 核心确认：Web 面板 /health
  HTTP_CODE="$(curl -s --noproxy '*' --max-time 8 -o /dev/null -w '%{http_code}' http://localhost:9120/health 2>/dev/null || true)"
  if [ "$HTTP_CODE" = "200" ]; then
    ok "Web 面板 http://localhost:9120 已就绪 (HTTP 200)"
  else
    warn "Web 面板 http://localhost:9120 暂未响应 (HTTP ${HTTP_CODE:-无响应})，可稍后运行: bash metano.sh status"
  fi
fi

# ════════════════════════════════════════════════════════════════════════════
# 8. 完成输出
# ════════════════════════════════════════════════════════════════════════════
info "[8/8] 完成"
echo ""
printf '┌───────────────────────────────────────────────────────────┐\n'
printf '│  metano 安装完成                                          │\n'
printf '├───────────────────────────────────────────────────────────┤\n'
printf '│  数据目录  : %-45s│\n' "$METANO_HOME"
printf '│  Web 面板  : %-45s│\n' "http://localhost:9120"
printf '│  健康检查  : %-45s│\n' "bash healthcheck.sh"
printf '│  服务管理  : %-45s│\n' "bash metano.sh {start|stop|status|restart}"
printf '└───────────────────────────────────────────────────────────┘\n'
echo ""
printf '  初始管理员 : admin\n'
if [ -n "${HERMES_DEFAULT_PASSWORD:-}" ] && ! _is_placeholder "$HERMES_DEFAULT_PASSWORD"; then
  printf '  初始密码   : %s（来自 HERMES_DEFAULT_PASSWORD）\n' "$HERMES_DEFAULT_PASSWORD"
elif [ -f "$CONFIG_FILE" ]; then
  printf '  初始密码   : 见 %s 中 auth.users（bcrypt 哈希，明文由首次启动日志输出）\n' "$CONFIG_FILE"
else
  printf '  初始密码   : 首次启动 Web 时自动生成（随机），见启动日志；或预先设置 HERMES_DEFAULT_PASSWORD\n'
fi
echo ""
echo "  下一步:"
echo "    1. 配置 LLM key 与消息渠道（未配置时运行: python3 gen_config.py --wizard；或见上方第 6 步提示 / .env.example）"
echo "    2. 浏览器打开 http://localhost:9120 并登录"
echo "    3. 查看 README.md 的「配置引导」节做自查清单"
echo ""
