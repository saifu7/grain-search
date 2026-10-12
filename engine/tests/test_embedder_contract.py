"""Checks GemmaEmbedder follows the EmbeddingGemma 2 model-card rules, using a mock
SentenceTransformer (so it runs without torch or the model download)."""

from __future__ import annotations

import sys
import tempfile
import types
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


class _Dtype:
    def __init__(self, name):
        self.name = name

    def __str__(self):
        return f"torch.{self.name}"


CALLS: dict = {"init": None, "encode": []}


class FakeST:
    def __init__(self, model_id, **kw):
        CALLS["init"] = (model_id, kw)

    def encode(self, inputs, **kw):
        CALLS["encode"].append((inputs, kw))
        return np.ones((len(inputs), kw["truncate_dim"]), np.float32)


def install_fakes(cuda: bool, bf16: bool):
    torch = types.ModuleType("torch")
    torch.bfloat16, torch.float32, torch.float16 = _Dtype("bfloat16"), _Dtype("float32"), _Dtype("float16")
    torch.version = types.SimpleNamespace(cuda="12.8")
    CALLS["fraction"], CALLS["emptied"] = None, 0
    torch.cuda = types.SimpleNamespace(
        is_available=lambda: cuda,
        is_bf16_supported=lambda: bf16,
        empty_cache=lambda: CALLS.__setitem__("emptied", CALLS["emptied"] + 1),
        set_per_process_memory_fraction=lambda f: CALLS.__setitem__("fraction", f),
        memory_reserved=lambda: CALLS.get("reserved", 0),
    )
    st = types.ModuleType("sentence_transformers")
    st.SentenceTransformer = FakeST
    sys.modules["torch"], sys.modules["sentence_transformers"] = torch, st
    sys.modules.setdefault("torchvision", types.ModuleType("torchvision"))
    return torch


class BrokenST:
    loads = 0

    def __init__(self, *a, **kw):
        BrokenST.loads += 1
        raise ValueError("boom: bad weights")


class OomST(FakeST):
    """Pretends the GPU can only fit batches of `fits` items."""

    fits = 2
    sizes: list = []

    def encode(self, inputs, **kw):
        OomST.sizes.append(kw["batch_size"])
        if kw["batch_size"] > OomST.fits:
            raise RuntimeError("CUDA error: out of memory")
        return super().encode(inputs, **kw)


class PickyImageST(FakeST):
    """Fails on images that are 33 px wide; `oom=True` makes every image call run out of GPU memory."""

    oom = False

    def encode(self, inputs, **kw):
        if PickyImageST.oom:
            raise RuntimeError("CUDA error: out of memory")
        for it in inputs:
            if isinstance(it, dict) and it["image"].size[0] == 33:
                raise ValueError("processor cannot handle this image")
        return super().encode(inputs, **kw)


class ContractTest(unittest.TestCase):
    def _images(self, widths):
        from PIL import Image

        tmp = Path(tempfile.mkdtemp())
        paths = []
        for i, w in enumerate(widths):
            Image.new("RGB", (w, 48), (i * 40 % 255, 80, 120)).save(tmp / f"i{i}.png")
            paths.append(str(tmp / f"i{i}.png"))
        return paths

    def test_one_bad_image_gets_its_real_error(self):
        install_fakes(cuda=False, bf16=False)
        sys.modules["sentence_transformers"].SentenceTransformer = PickyImageST
        PickyImageST.oom = False
        paths = self._images([60, 33, 70])
        e = self.make()
        out = e.encode_images(paths)
        self.assertTrue(out[0].any() and out[2].any())
        self.assertFalse(out[1].any())
        self.assertIn("processor cannot handle this image", e.image_errors[paths[1]])
        self.assertEqual(set(e.image_errors), {paths[1]})

    def test_tiny_images_never_reach_the_model(self):
        install_fakes(cuda=False, bf16=False)
        sys.modules["sentence_transformers"].SentenceTransformer = PickyImageST
        PickyImageST.oom = False
        paths = self._images([60, 70])
        from PIL import Image

        Image.new("RGB", (400, 1)).save(Path(paths[0]).with_name("line.png"))
        line = str(Path(paths[0]).with_name("line.png"))
        CALLS["encode"].clear()
        e = self.make()
        out = e.encode_images([paths[1], line])
        self.assertTrue(out[0].any())
        self.assertFalse(out[1].any())
        self.assertIn("too small", e.image_errors[line])
        sent = [it for inputs, _ in CALLS["encode"] for it in inputs]
        self.assertTrue(all(min(it["image"].size) >= 32 for it in sent))

    def test_whole_batch_failing_is_a_model_error(self):
        install_fakes(cuda=False, bf16=False)
        sys.modules["sentence_transformers"].SentenceTransformer = PickyImageST
        PickyImageST.oom = False
        with self.assertRaisesRegex(RuntimeError, "every image in the batch failed"):
            self.make().encode_images(self._images([33, 33, 33]))

    def test_gpu_full_on_images_is_raised_not_hidden(self):
        install_fakes(cuda=True, bf16=True)
        sys.modules["sentence_transformers"].SentenceTransformer = PickyImageST
        PickyImageST.oom = True
        try:
            with self.assertRaisesRegex(RuntimeError, "out of memory"):
                self.make().encode_images(self._images([60, 70]))
        finally:
            PickyImageST.oom = False

    def test_gpu_oom_retries_with_smaller_batches(self):
        install_fakes(cuda=True, bf16=True)
        sys.modules["sentence_transformers"].SentenceTransformer = OomST
        OomST.sizes, OomST.fits = [], 2
        e = self.make(text_batch=8)
        out = e.encode_documents([("a", "x")] * 5)
        self.assertEqual(out.shape[0], 5)
        self.assertEqual(OomST.sizes, [8, 4, 2])

    def test_gpu_oom_at_batch_one_is_raised(self):
        install_fakes(cuda=True, bf16=True)
        sys.modules["sentence_transformers"].SentenceTransformer = OomST
        OomST.sizes, OomST.fits = [], 0
        e = self.make(text_batch=4)
        with self.assertRaisesRegex(RuntimeError, "out of memory"):
            e.encode_documents([("a", "x")])
        self.assertEqual(OomST.sizes[-1], 1)

    def test_failed_load_is_not_retried(self):
        install_fakes(cuda=False, bf16=False)
        sys.modules["sentence_transformers"].SentenceTransformer = BrokenST
        BrokenST.loads = 0
        e = self.make()
        for _ in range(3):
            with self.assertRaises(RuntimeError) as cm:
                e.encode_query("x")
        self.assertIn("boom: bad weights", str(cm.exception))
        self.assertEqual(BrokenST.loads, 1)

    def tearDown(self):
        sys.modules.pop("torch", None)
        sys.modules.pop("sentence_transformers", None)
        if isinstance(sys.modules.get("torchvision"), types.ModuleType) and not hasattr(
            sys.modules["torchvision"], "__file__"
        ):
            sys.modules.pop("torchvision", None)
        CALLS["encode"].clear()

    def test_missing_torchvision_is_explained(self):
        install_fakes(cuda=False, bf16=False)
        sys.modules["torchvision"] = None  # simulates "not installed"
        try:
            with self.assertRaisesRegex(RuntimeError, "missing torchvision"):
                self.make().encode_query("x")
        finally:
            sys.modules.pop("torchvision", None)

    def make(self, **cfg):
        from grain_search.config import ModelCfg
        from grain_search.embedder import GemmaEmbedder

        return GemmaEmbedder(ModelCfg(**cfg))

    def test_gpu_bf16_and_prompts(self):
        torch = install_fakes(cuda=True, bf16=True)
        e = self.make(dim=512)
        q = e.encode_query("dog on a sofa")
        self.assertEqual(q.shape, (512,))
        model_id, kw = CALLS["init"]
        self.assertEqual(model_id, "google/embeddinggemma-2")
        self.assertIs(kw["model_kwargs"]["dtype"], torch.bfloat16)
        self.assertEqual(kw["config_kwargs"], {"audio_config": None})
        inputs, ekw = CALLS["encode"][-1]
        self.assertEqual(ekw["prompt_name"], "SearchQuery")
        self.assertEqual((ekw["truncate_dim"], ekw["normalize_embeddings"]), (512, True))

        e.encode_documents([("notes.txt", "hello world"), ("", "x")])
        inputs, ekw = CALLS["encode"][-1]
        self.assertEqual(inputs, ["title: notes.txt | text: hello world", "title: none | text: x"])
        self.assertNotIn("prompt_name", ekw)

    def test_gpu_memory_is_capped_and_released(self):
        install_fakes(cuda=True, bf16=True)
        CALLS["reserved"] = 3 * 1024**3
        e = self.make(gpu_memory_fraction=0.4, max_seq_tokens=512)
        e.encode_query("x")
        self.assertEqual(CALLS["fraction"], 0.4)
        self.assertEqual(e._model.max_seq_length, 512)
        self.assertGreaterEqual(CALLS["emptied"], 1)  # above 2 GB reserved -> handed back
        CALLS["reserved"], CALLS["emptied"] = 0, 0
        e.encode_query("y")
        self.assertEqual(CALLS["emptied"], 0)

    def test_cpu_float32_never_float16(self):
        torch = install_fakes(cuda=False, bf16=False)
        e = self.make(images=False)
        e.encode_query("x")
        _, kw = CALLS["init"]
        self.assertIs(kw["model_kwargs"]["dtype"], torch.float32)
        self.assertEqual(kw["config_kwargs"], {"audio_config": None, "vision_config": None})
        self.assertEqual(kw["device"], "cpu")

    def test_old_gpu_without_bf16_uses_float32(self):
        torch = install_fakes(cuda=True, bf16=False)
        self.make().encode_query("x")
        self.assertIs(CALLS["init"][1]["model_kwargs"]["dtype"], torch.float32)

    def test_images_no_prefix_and_bad_files_zero(self):
        install_fakes(cuda=False, bf16=False)
        from PIL import Image

        tmp = Path(tempfile.mkdtemp())
        Image.new("RGB", (4000, 3000), (1, 2, 3)).save(tmp / "big.jpg")
        (tmp / "bad.jpg").write_bytes(b"nope")
        e = self.make(vision_tokens=140)
        out = e.encode_images([str(tmp / "big.jpg"), str(tmp / "bad.jpg")])
        self.assertEqual(out.shape, (2, 768))
        self.assertTrue(out[0].any())
        self.assertFalse(out[1].any())
        inputs, ekw = CALLS["encode"][-1]
        self.assertEqual(list(inputs[0].keys()), ["image"])  # media: no text prefix
        self.assertLessEqual(max(inputs[0]["image"].size), 1536)  # downscaled
        self.assertEqual(ekw["processing_kwargs"], {"image": {"max_soft_tokens": 140}})
        self.assertNotIn("prompt_name", ekw)


if __name__ == "__main__":
    unittest.main()
