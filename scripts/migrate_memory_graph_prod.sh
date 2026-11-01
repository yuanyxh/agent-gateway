#!/bin/bash
# Backfill memory_graph.sqlite from the hot and cold Qdrant payloads.
# Run with the app stopped: Qdrant local mode holds an exclusive lock.
# Idempotent: nodes upsert by stable id, edges dedupe by (from, to, type).
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
QDRANT="${QDRANT_PATH:-}"
HOME_DIR="${GATEWAY_HOME:-}"
PYTHON_BIN="${PYTHON:-}"
GRAPH_PATH=""
PORT="${MCP_PORT:-}"
DRY_RUN=0
SKIP_BACKUP=0
FORCE=0

usage() {
  cat <<'EOF'
用法：migrate_memory_graph_prod.sh [选项]

  --qdrant PATH    Qdrant 根目录（默认 $QDRANT_PATH）
  --home PATH      GATEWAY_HOME（默认 $GATEWAY_HOME）
  --python PATH    解释器（默认 $GATEWAY_HOME/mcp-venv/bin/python）
  --graph PATH     图库文件（默认 $GATEWAY_HOME/memory_graph.sqlite）
  --port PORT      MCP 端口；提供时检查是否仍在监听（默认 $MCP_PORT）
  --dry-run        只统计计划写入的数据，不落盘、不备份
  --skip-backup    跳过备份（不建议）
  --force          跳过端口与进程检查（仅当你确认服务已停）
  -h, --help       显示本说明
EOF
}

die() {
  echo "错误：$*" >&2
  exit 1
}

while [ $# -gt 0 ]; do
  case "$1" in
    --qdrant) QDRANT="${2:-}"; shift 2 ;;
    --home) HOME_DIR="${2:-}"; shift 2 ;;
    --python) PYTHON_BIN="${2:-}"; shift 2 ;;
    --graph) GRAPH_PATH="${2:-}"; shift 2 ;;
    --port) PORT="${2:-}"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    --skip-backup) SKIP_BACKUP=1; shift ;;
    --force) FORCE=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "未知参数：$1" ;;
  esac
done

[ -n "$QDRANT" ] || die "缺少 Qdrant 根目录：--qdrant 或 QDRANT_PATH"
[ -d "$QDRANT" ] || die "Qdrant 目录不存在：$QDRANT"
[ -n "$HOME_DIR" ] || die "缺少 GATEWAY_HOME：--home 或 GATEWAY_HOME"
[ -d "$HOME_DIR" ] || die "GATEWAY_HOME 不存在：$HOME_DIR"
[ -n "$PYTHON_BIN" ] || PYTHON_BIN="$HOME_DIR/mcp-venv/bin/python"
[ -x "$PYTHON_BIN" ] || die "解释器不可执行：$PYTHON_BIN"
GRAPH_PATH="${GRAPH_PATH:-$HOME_DIR/memory_graph.sqlite}"
[ -f "$REPO/scripts/migrate_memory_graph.py" ] || die "找不到迁移脚本：$REPO/scripts/migrate_memory_graph.py"

if [ "$FORCE" -eq 0 ]; then
  if [ -n "$PORT" ] && lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    die "端口 $PORT 仍在监听，先退出 Utility"
  fi
  if pgrep -f "agent_gateway.services.mcp.server" >/dev/null 2>&1; then
    die "MCP 服务进程仍在运行，先退出 Utility"
  fi
fi

STAMP="$(date +%Y%m%d_%H%M%S)"
BACKUP="$HOME_DIR/backup-memory-graph-$STAMP"
DRY_OUT="$(mktemp /tmp/memory_graph_dry.XXXXXX)"
LIVE_OUT="$(mktemp /tmp/memory_graph_live.XXXXXX)"
trap 'rm -f "$DRY_OUT" "$LIVE_OUT"' EXIT

run_migrate() {
  GATEWAY_HOME="$HOME_DIR" QDRANT_PATH="$QDRANT" PYTHONPATH="$REPO/src" \
    "$PYTHON_BIN" "$REPO/scripts/migrate_memory_graph.py" --db "$GRAPH_PATH" "$@"
}

echo "== 1/4 预演 =="
run_migrate --dry-run | tee "$DRY_OUT"

if [ "$DRY_RUN" -eq 1 ]; then
  echo "预演完成，未写入。"
  exit 0
fi

if [ "$SKIP_BACKUP" -eq 0 ]; then
  echo "== 2/4 备份 =="
  NEED_KB="$(du -sk "$QDRANT" | awk '{print $1}')"
  AVAIL_KB="$(df -k "$HOME_DIR" | awk 'NR==2 {print $4}')"
  [ "$AVAIL_KB" -ge "$NEED_KB" ] || die "备份空间不足：需要 ${NEED_KB}KB，可用 ${AVAIL_KB}KB"
  mkdir -p "$BACKUP"
  cp -a "$QDRANT" "$BACKUP/qdrant_data"
  for suffix in "" "-wal" "-shm"; do
    if [ -f "$GRAPH_PATH$suffix" ]; then
      cp -a "$GRAPH_PATH$suffix" "$BACKUP/"
    fi
  done
  echo "备份目录：$BACKUP"
else
  echo "== 2/4 备份已跳过 =="
fi

echo "== 3/4 写入 =="
run_migrate | tee "$LIVE_OUT"

echo "== 4/4 校验 =="
"$PYTHON_BIN" - "$GRAPH_PATH" "$DRY_OUT" "$LIVE_OUT" <<'PY'
import json
import sqlite3
import sys

graph_path, dry_path, live_path = sys.argv[1:4]
planned = json.load(open(dry_path, encoding="utf-8"))
written = json.load(open(live_path, encoding="utf-8"))
if planned.get("nodes") != written.get("nodes"):
    sys.exit(f"计划节点数与实际不一致：{planned.get('nodes')} != {written.get('nodes')}")
con = sqlite3.connect(graph_path)
nodes = con.execute("SELECT COUNT(*) FROM memory_nodes").fetchone()[0]
edges = con.execute("SELECT COUNT(*) FROM memory_edges").fetchone()[0]
con.close()
print(json.dumps({"db_nodes": nodes, "db_edges": edges}, ensure_ascii=False))
if nodes == 0:
    sys.exit("图库为空，迁移未生效")
PY

echo "完成。"
