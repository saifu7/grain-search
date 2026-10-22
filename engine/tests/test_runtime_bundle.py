"""scripts/runtime_bundle.py: what the installer carries so the setup window can fetch the rest."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import runtime_bundle as rb  # noqa: E402

EXPORT = """\
anyio==4.15.1
colorama==0.4.6 ; sys_platform == 'win32'
nvidia-cublas-cu12==12.8.4.1 ; sys_platform == 'linux'
cuda-toolkit==12.8.1 ; sys_platform == 'linux'
numpy==2.5.4 ; python_full_version >= '3.12'
torch==2.11.0+cu128 ; sys_platform == 'linux' or sys_platform == 'win32'
torchvision==0.26.0+cu128 ; sys_platform == 'win32'
triton==3.6.0 ; sys_platform == 'linux'
transformers==5.19.0
"""


class PureTest(unittest.TestCase):
    def test_requirements_drop_pytorch_family(self):
        reqs = rb.requirements(EXPORT).splitlines()
        self.assertIn("anyio==4.15.1", reqs)
        self.assertIn("colorama==0.4.6 ; sys_platform == 'win32'", reqs)
        self.assertIn("transformers==5.19.0", reqs)
        joined = "\n".join(reqs)
        for gone in ("torch", "torchvision", "nvidia-", "cuda-", "triton"):
            self.assertNotIn(gone + ("==" if gone in ("torch", "torchvision", "triton") else ""), joined)

    def test_torch_pins_from_the_real_lock(self):
        pins = rb.torch_pins((ROOT / "engine" / "uv.lock").read_text("utf-8"))
        self.assertNotIn("+", pins["torch"])
        self.assertNotIn("+", pins["torchvision"])
        self.assertTrue(pins["cuda_index"].startswith("https://download.pytorch.org/whl/cu"))

    def test_fingerprint_changes_only_with_downloads(self):
        pins = {"torch": "2.11.0", "torchvision": "0.26.0"}
        a = rb.fingerprint("x==1\n", pins)
        self.assertEqual(a, rb.fingerprint("x==1\n", dict(pins)))
        self.assertNotEqual(a, rb.fingerprint("x==2\n", pins))
        self.assertNotEqual(a, rb.fingerprint("x==1\n", {**pins, "torch": "2.12.0"}))


class BundleTest(unittest.TestCase):
    """build_bundle with a stand-in for `uv build` (needs the network) and the real `uv export`."""

    def test_bundle_contents(self):
        uv = shutil.which("uv")
        if not uv:
            self.skipTest("uv not on PATH")
        out = Path(tempfile.mkdtemp()) / "engine"

        def runner(cmd, cwd=None):
            if cmd[1] == "build":
                Path(cmd[cmd.index("--out-dir") + 1], "grain_search-9.9.9-py3-none-any.whl").write_bytes(b"PK")
                return ""
            return subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True).stdout

        try:
            info = rb.build_bundle(out, uv, runner=runner)
            names = {p.name for p in out.iterdir()}
            self.assertTrue({"runtime.json", "requirements.txt", "grain_search-9.9.9-py3-none-any.whl"} <= names)
            self.assertTrue(any(n.startswith("uv") for n in names))
            saved = json.loads((out / "runtime.json").read_text("utf-8"))
            self.assertEqual(saved, info)
            self.assertEqual(saved["python"], rb.PYTHON)
            self.assertEqual(saved["wheel"], "grain_search-9.9.9-py3-none-any.whl")
            self.assertEqual(saved["torch_index"]["cpu"], rb.CPU_INDEX)
            reqs = (out / "requirements.txt").read_text("utf-8")
            self.assertIn("sentence-transformers==", reqs)
            self.assertNotIn("\ntorch==", "\n" + reqs)
        finally:
            shutil.rmtree(out.parent, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
