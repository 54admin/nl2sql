#!/usr/bin/env bash
# 批量灌知识库：遍历目录下所有 .md/.txt，逐个上传分段 embedding 入库。
# 用法：./upload_kb.sh <文档目录> [分类]
#   分类：manual 手册 / policy 政策 / case 案例 / general 通用（默认 general）
# 例： ./upload_kb.sh ./运维手册 manual
#      ./upload_kb.sh ./调度政策 policy
set -u
DIR="${1:-./kb-docs}"
CATEGORY="${2:-general}"
HOST="${NL2SQL_HOST:-http://127.0.0.1:8000}"

if [ ! -d "$DIR" ]; then echo "目录不存在: $DIR"; exit 1; fi

ok=0; fail=0; failed_files=""
shopt -s nullglob nocaseglob
files=( "$DIR"/*.md "$DIR"/*.txt "$DIR"/*.csv "$DIR"/*.docx "$DIR"/*.xlsx )
echo "发现 ${#files[@]} 个文档（支持 md/txt/csv/docx/xlsx，后端自动解析），灌入 $HOST ..."
for f in "${files[@]}"; do
  printf "  %-40s " "$(basename "$f")"
  code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 120 \
    -F "file=@$f" -F "category=$CATEGORY" "$HOST/api/admin/knowledge/upload")
  if [ "$code" = "200" ]; then echo "✅"; ok=$((ok+1))
  else echo "❌ HTTP $code"; fail=$((fail+1)); failed_files+="  $f\n"; fi
done
echo ""
echo "完成：成功 $ok / 失败 $fail"
[ $fail -gt 0 ] && echo -e "失败文件:\n$failed_files"
echo "查看入库：curl -s $HOST/api/admin/knowledge/docs | python3 -m json.tool"
