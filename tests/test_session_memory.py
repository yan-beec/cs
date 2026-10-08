"""Check that a new consultation cannot inherit messages or a saved summary."""
import unittest

from langgraph.checkpoint.memory import InMemorySaver

from ics_agent.rag.graph.edges import build_rag_graph


class SessionMemoryIsolationTest(unittest.IsolatedAsyncioTestCase):
    async def test_new_thread_starts_without_old_messages_or_summary(self):
        graph = build_rag_graph(checkpointer=InMemorySaver())
        old_config = {"configurable": {"thread_id": "chatwoot:1:56:round:old"}}
        first = await graph.ainvoke(
            {
                "query": "旧问题：账号无法登录",
                "memory_enabled": True,
                "answer_override": "请先检查验证码。",
                "conversation_summary": "旧咨询：账号登录异常。",
            },
            config=old_config,
        )
        followup = await graph.ainvoke(
            {
                "query": "还是不行",
                "memory_enabled": True,
                "answer_override": "请检查手机号。",
            },
            config=old_config,
        )
        self.assertEqual(first["conversation_turn_count"], 1)
        self.assertEqual(followup["conversation_turn_count"], 2)
        self.assertIn("旧问题", str(followup["recent_messages"]))
        self.assertIn("登录异常", followup["conversation_summary"])

        fresh = await graph.ainvoke(
            {
                "query": "新问题：怎样修改昵称",
                "memory_enabled": True,
                "answer_override": "进入个人资料修改。",
            },
            config={"configurable": {"thread_id": "chatwoot:1:56:round:new"}},
        )
        self.assertEqual(fresh["conversation_turn_count"], 1)
        self.assertEqual(fresh["conversation_summary"], "")
        self.assertEqual(len(fresh["recent_messages"]), 2)
        self.assertNotIn("旧问题", str(fresh))
        self.assertNotIn("登录异常", str(fresh))


if __name__ == "__main__":
    unittest.main()
