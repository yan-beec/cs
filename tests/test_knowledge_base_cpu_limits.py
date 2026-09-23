import os
import unittest
from unittest.mock import patch

from knowledge_base_in.build_chromadb import BGEM3Embedder
from knowledge_base_in.parse_documents import _build_ocr_runtime_params


class _FakeTorch:
    def __init__(self, *, reject_interop: bool = False) -> None:
        self.num_threads: int | None = None
        self.interop_threads: int | None = None
        self.reject_interop = reject_interop

    def set_num_threads(self, value: int) -> None:
        self.num_threads = value

    def set_num_interop_threads(self, value: int) -> None:
        if self.reject_interop:
            raise RuntimeError("parallel work has already started")
        self.interop_threads = value


class KnowledgeBaseCpuLimitTests(unittest.TestCase):
    def test_cpu_threads_are_capped_by_host_cpu_count(self) -> None:
        torch = _FakeTorch()
        with patch("knowledge_base_in.build_chromadb.os.cpu_count", return_value=2):
            result = BGEM3Embedder._configure_cpu_threads(
                torch,
                requested_threads=8,
                requested_interop_threads=4,
            )

        self.assertEqual((2, 2), result)
        self.assertEqual(2, torch.num_threads)
        self.assertEqual(2, torch.interop_threads)

    def test_interop_limit_failure_does_not_disable_intraop_limit(self) -> None:
        torch = _FakeTorch(reject_interop=True)
        with patch("knowledge_base_in.build_chromadb.os.cpu_count", return_value=8):
            result = BGEM3Embedder._configure_cpu_threads(
                torch,
                requested_threads=2,
                requested_interop_threads=1,
            )

        self.assertEqual((2, 1), result)
        self.assertEqual(2, torch.num_threads)
        self.assertIsNone(torch.interop_threads)

    def test_cpu_environment_disables_nested_parallelism(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            BGEM3Embedder._configure_cpu_environment(2)
            self.assertEqual("2", os.environ["OMP_NUM_THREADS"])
            self.assertEqual("2", os.environ["MKL_NUM_THREADS"])
            self.assertEqual("false", os.environ["TOKENIZERS_PARALLELISM"])

    def test_ocr_runtime_params_limit_onnxruntime_threads(self) -> None:
        self.assertEqual(
            {
                "EngineConfig.onnxruntime.intra_op_num_threads": 2,
                "EngineConfig.onnxruntime.inter_op_num_threads": 1,
            },
            _build_ocr_runtime_params(2, 1),
        )


if __name__ == "__main__":
    unittest.main()
