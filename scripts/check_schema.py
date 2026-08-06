"""防漂移校验：ORM 生成的 DDL 必须与 db/schema.sql 完全一致。

改了 ORM 模型后必须重跑 `python3 scripts/gen_schema.py` 再提交，否则本校验 fail。
提交前/CI 跑：python3 scripts/check_schema.py
"""
import sys
import difflib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.gen_schema import generate  # noqa: E402


def main() -> int:
    committed = (Path(__file__).resolve().parent.parent / "db" / "schema.sql").read_text(encoding="utf-8")
    generated = generate()
    if committed == generated:
        print("OK: db/schema.sql 与 ORM 模型一致")
        return 0
    diff = "\n".join(difflib.unified_diff(
        committed.splitlines(), generated.splitlines(),
        fromfile="db/schema.sql (committed)", tofile="ORM (generated)", lineterm=""))
    print("FAIL: db/schema.sql 与 ORM 模型不一致！改了 ORM 后请重跑:\n"
          "    python3 scripts/gen_schema.py\n\n" + diff[:2000])
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
