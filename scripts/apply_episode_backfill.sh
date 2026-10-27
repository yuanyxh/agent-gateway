#!/bin/bash
# Backfill episode_id on a copy, verify, then swap it in. Run with utility stopped:
# Qdrant local mode holds an exclusive lock and the swap replaces the directory.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
QDRANT_ROOT="${QDRANT_ROOT:-$HOME/Documents/selfdata/database/qdrant_data}"
MEM0_DIR="$QDRANT_ROOT/mem0"
BACKUP_ROOT="${BACKUP_ROOT:-$(dirname "$QDRANT_ROOT")/qdrant_data_backup}"
STAMP="$(date +%Y%m%d_%H%M%S)"
BACKUP_DIR="$BACKUP_ROOT/episode_$STAMP"
PY="${PYTHON:-$REPO/.venv/bin/python}"

if lsof -nP -iTCP:9001 -sTCP:LISTEN >/dev/null 2>&1; then
  echo "错误：9001 仍在监听，先停止 utility" >&2
  exit 1
fi
if pgrep -f "agent_gateway.services.mcp.server" >/dev/null 2>&1; then
  echo "错误：MCP 服务进程仍在运行，先停止 utility" >&2
  exit 1
fi
if [ ! -d "$MEM0_DIR/collection" ]; then
  echo "错误：找不到 $MEM0_DIR/collection" >&2
  exit 1
fi

mkdir -p "$BACKUP_DIR"
WORK="$(mktemp -d /tmp/episode_backfill.XXXXXX)"
echo "临时工作区：$WORK"
echo "备份目录：$BACKUP_DIR"

echo "== 1/5 复制记忆库 =="
cp -R "$MEM0_DIR" "$WORK/mem0"

echo "== 2/5 回填 =="
"$PY" "$REPO/scripts/backfill_episode.py" \
  --path "$WORK/mem0" \
  --manifest "$BACKUP_DIR/manifest.json" | tee "$BACKUP_DIR/stats.json"

echo "== 3/5 校验 =="
"$PY" - "$WORK/mem0" <<'PY' | tee "$BACKUP_DIR/verify.json"
import json
import sys
from collections import Counter

from qdrant_client import QdrantClient

client = QdrantClient(path=sys.argv[1])
points, _ = client.scroll("mem0", limit=20000, with_payload=True, with_vectors=False)
active = [
    p
    for p in points
    if ((p.payload or {}).get("status") or "active") == "active"
    and not (p.payload or {}).get("archived")
]
filled = [p for p in active if ((p.payload or {}).get("episode_id") or "").strip()]
sizes = Counter((p.payload or {})["episode_id"] for p in filled)
counts = sorted(sizes.values(), reverse=True)
result = {
    "points": len(points),
    "active": len(active),
    "filled": len(filled),
    "groups": len(sizes),
    "max_size": counts[0] if counts else 0,
    "groups_ge_10": sum(1 for x in counts if x >= 10),
}
print(json.dumps(result, ensure_ascii=False))
client.close()
if len(filled) != len(active):
    sys.exit(2)
PY

echo "== 4/5 替换 =="
cp -R "$WORK/mem0" "$QDRANT_ROOT/mem0.new"
mv "$MEM0_DIR" "$BACKUP_DIR/mem0.orig"
mv "$QDRANT_ROOT/mem0.new" "$MEM0_DIR"

echo "== 5/5 完成 =="
echo "已替换：$MEM0_DIR"
echo "原目录备份：$BACKUP_DIR/mem0.orig"
echo "回滚命令："
echo "  mv \"$MEM0_DIR\" \"$BACKUP_DIR/mem0.backfilled\""
echo "  mv \"$BACKUP_DIR/mem0.orig\" \"$MEM0_DIR\""
