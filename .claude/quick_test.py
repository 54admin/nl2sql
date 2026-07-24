"""Hook 脚本：PostToolUse(Edit/Write) 后，根据改的文件跑对应 tests/test_<basename>.py。
秒完（单文件），不存在则跳过。stdin 收 Claude Code 传的 JSON（含 tool_input.file_path）。"""
import sys, json, subprocess, os

try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)   # stdin 非JSON，静默跳过

fp = d.get("tool_input", {}).get("file_path", "")
if not fp or not fp.endswith(".py"):
    sys.exit(0)

b = os.path.basename(fp).replace(".py", "")
test = f"tests/test_{b}.py"
if not os.path.exists(test):
    sys.exit(0)   # 无对应测试，跳过（不报错不阻塞）

r = subprocess.run(["python3", "-m", "pytest", test, "-x", "-q"], capture_output=True, text=True)
out = (r.stdout or "") + (r.stderr or "")
# 只输出最后 800 字符（结果摘要），不刷屏
print(out[-800:] if out else "(pytest 无输出)")
