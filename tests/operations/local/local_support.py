import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
LOCAL = ROOT / "scripts/operations/local"
sys.path.insert(0, str(LOCAL))

import bootstrap as bootstrap  # noqa: E402
import common as common  # noqa: E402
import prepare as prepare  # noqa: E402

import images as images  # noqa: E402


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gate = load("local_cluster_gate", ROOT / "scripts/harness/local/cluster-gate.py")
child = load("local_bootstrap_child", LOCAL / "bootstrap-child.py")
server = load("local_module_server", LOCAL / "module-server.py")
