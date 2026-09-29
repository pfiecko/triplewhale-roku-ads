import importlib.util
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.environ.setdefault("CONFIG_PATH", str(Path(__file__).with_name("config.test.yaml")))
sys.path.insert(0, str(ROOT / "lib"))


def load_function(folder: str):
    """Import <folder>/main.py under a unique name (every function has a main.py)."""
    fn_dir = ROOT / folder
    sys.path.insert(0, str(fn_dir))
    spec = importlib.util.spec_from_file_location(f"{folder.replace('-', '_')}_main", fn_dir / "main.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
