from pathlib import Path
import subprocess
import sys

root = Path(__file__).resolve().parents[1]
out = root / "runner_engine" / "generated"
out.mkdir(parents=True, exist_ok=True)
subprocess.check_call([
    sys.executable, "-m", "grpc_tools.protoc",
    f"-I{root / 'proto'}",
    f"--python_out={out}",
    f"--grpc_python_out={out}",
    str(root / "proto" / "runner.proto"),
])

# grpc_tools generates `import runner_pb2`; make it package-relative.
grpc_file = out / "runner_pb2_grpc.py"
text = grpc_file.read_text(encoding="utf-8")
text = text.replace("import runner_pb2 as runner__pb2", "from . import runner_pb2 as runner__pb2")
grpc_file.write_text(text, encoding="utf-8")
print(out)
