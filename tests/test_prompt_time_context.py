import unittest

from ics_agent.rag.prompt import build_dynamic_prompt


class PromptTimeContextTest(unittest.TestCase):
    def test_injects_runtime_time_context(self) -> None:
        prompt = build_dynamic_prompt(
            query="今天是周几？",
            documents=[],
            intent_label="闲聊",
            current_time_context=(
                "当前日期时间：2026-09-28 17:30:00\n"
                "当前日期：2026-09-28\n"
                "当前时间：17:30:00\n"
                "当前星期：星期一\n"
                "时区：Asia/Shanghai"
            ),
        )

        self.assertIn("【当前时间】", prompt)
        self.assertIn("当前日期时间：2026-09-28 17:30:00", prompt)
        self.assertIn("当前星期：星期一", prompt)
        self.assertIn("产品规则、处理时效和业务口径仍以知识库参考内容为准", prompt)


if __name__ == "__main__":
    unittest.main()
