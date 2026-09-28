import unittest

from ics_agent.app.handoff import is_explicit_handoff_request


class HandoffIntentTest(unittest.TestCase):
    def test_accepts_direct_handoff_requests(self) -> None:
        positives = [
            "人工",
            "转人工",
            "我要人工客服",
            "麻烦帮我转人工",
            "请转接人工客服处理一下",
            "能不能转人工",
        ]

        for text in positives:
            with self.subTest(text=text):
                self.assertTrue(is_explicit_handoff_request(text))

    def test_rejects_negated_handoff_mentions(self) -> None:
        negatives = [
            "不要转人工",
            "先不要转人工，我再看看",
            "不需要人工客服",
            "不用人工服务",
            "别给我转人工",
            "不要给我转人工",
        ]

        for text in negatives:
            with self.subTest(text=text):
                self.assertFalse(is_explicit_handoff_request(text))

    def test_rejects_informational_or_question_mentions(self) -> None:
        negatives = [
            "人工服务时间是几点",
            "人工客服入口在哪",
            "为什么总是转人工",
            "这个需要人工审核吗",
            "你是人工智能客服吗",
            "你是人工客服吗",
        ]

        for text in negatives:
            with self.subTest(text=text):
                self.assertFalse(is_explicit_handoff_request(text))


if __name__ == "__main__":
    unittest.main()
