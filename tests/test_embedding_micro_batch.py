import asyncio
import unittest

from ics_agent.rag.retrieval.dense_search import (
    _EmbeddingBatchBroker,
    _EmbeddingRequest,
)


class EmbeddingMicroBatchTest(unittest.IsolatedAsyncioTestCase):
    async def test_collects_queries_until_batch_limit(self) -> None:
        calls: list[list[str]] = []

        def embed(texts: list[str]) -> list[list[float]]:
            calls.append(texts)
            return [[float(index)] for index, _ in enumerate(texts)]

        broker = _EmbeddingBatchBroker(
            embed,
            max_batch_size=4,
            max_wait_ms=200,
        )
        futures = [
            asyncio.get_running_loop().create_future()
            for _ in range(4)
        ]
        for index, future in enumerate(futures):
            broker.submit(
                _EmbeddingRequest(text=f"query-{index}", future=future)
            )

        vectors = await asyncio.gather(*futures)

        self.assertEqual(calls, [["query-0", "query-1", "query-2", "query-3"]])
        self.assertEqual(vectors, [[0.0], [1.0], [2.0], [3.0]])

    async def test_flushes_partial_batch_after_wait_window(self) -> None:
        calls: list[list[str]] = []

        def embed(texts: list[str]) -> list[list[float]]:
            calls.append(texts)
            return [[1.0] for _ in texts]

        broker = _EmbeddingBatchBroker(
            embed,
            max_batch_size=4,
            max_wait_ms=20,
        )
        future = asyncio.get_running_loop().create_future()
        broker.submit(_EmbeddingRequest(text="single-query", future=future))

        self.assertEqual(await asyncio.wait_for(future, timeout=1), [1.0])
        self.assertEqual(calls, [["single-query"]])


if __name__ == "__main__":
    unittest.main()
