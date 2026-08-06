"""防漂移校验：代码常量生成的 seed 必须与 db/seed.sql 一致。

改了 scripts/gen_seed.py 的 SEED_SKILLS 常量后必须重跑 `python3 scripts/gen_seed.py` 再提交，否则本校验 fail。
用法: python3 scripts/check_seed.py
"""
import sys
import difflib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.gen_seed import generate  # noqa: E402


def main() -> int:
    committed = (Path(__file__).resolve().parent.parent / "db" / "seed.sql").read_text(encoding="utf-8")
    generated = generate()
    if committed == generated:
        print("OK: db/seed.sql 与 gen_seed.py 的 SEED_SKILLS 常量一致")
        return 0
    diff = "\n".join(difflib.unified_diff(
        committed.splitlines(), generated.splitlines(),
        fromfile="db/seed.sql (committed)", tofile="gen_seed.py (generated)", lineterm=""))
    print("FAIL: seed.sql 与 SEED_SKILLS 不一致！改了 gen_seed.py 后请重跑:\n"
          "    python3 scripts/gen_seed.py\n\n" + diff[:2000])
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
