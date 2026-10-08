"""分别记录首次调用和同一进程内连续调用的完整回复耗时。

运行：python -m ics_agent.llm_factory.response_time
"""

import asyncio
import statistics
import time
from datetime import datetime
from urllib.parse import urlsplit

from ics_agent.app.core.config import PROJECT_ROOT, get_settings
from ics_agent.llm_factory import call_cloud_llm


PROMPT = "今天天气真好啊。"
PROMPT2 = '''用户提问：“我给女神发消息要多少钱”
以下是检索到的回复依据：“### 5.1 关注

**常见问法**：怎么关注 / 怎么加好友 / 互相关注

**回答**：如遇到在意的 TA，可点击进入对方主页进行关注，互相关注后可在好友列表查看，文字聊天免费。
温馨提示：关注属于个人行为，无任何亲密度/等级/数量/礼物等限制。

### 5.2 私聊消息资费

**常见问法**：每次发消息多少 U 币 / 一条信息多少 u / 私聊怎么收费

**回答**：私聊消息的资费由主播设置，一般为 5—6 U 币/条。具体可在对方主页查看或咨询对方。

### 5.3 互关后是否免费聊天

**常见问法**：互关后可以免费聊天吗 / 互相关注的人聊天收费吗

**回答**：互相关注后文字聊天不收取资费；通话正常支付资费。”
'''
# 改为 "PROMPT2" 可测试检索回复；同一轮所有调用都使用相同提示词。
PROMPT_NAME = "PROMPT"
CONTINUOUS_CALLS = 3  # 首次调用之后，再连续调用的次数。
LOG_PATH = PROJECT_ROOT / "log" / "llm_response_time.log"


async def main() -> None:
    if CONTINUOUS_CALLS < 1:
        raise ValueError("CONTINUOUS_CALLS 必须至少为 1")
    prompt = {"PROMPT": PROMPT, "PROMPT2": PROMPT2}[PROMPT_NAME]
    settings = get_settings()
    endpoint = urlsplit(settings.llm_base_url).hostname or "默认端点"
    batch = datetime.now().astimezone().isoformat(timespec="microseconds")
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    durations = []

    print(f"开始时间：{batch}", flush=True)
    print(f"模型：{settings.llm_model}（{endpoint}），提示词：{PROMPT_NAME}", flush=True)
    print(f"首次调用 1 次，再连续调用 {CONTINUOUS_CALLS} 次。", flush=True)
    print("首次包含客户端初始化；连续调用复用客户端。耗时均截至完整回复返回。", flush=True)

    with LOG_PATH.open("a", encoding="utf-8") as log_file:
        for index in range(CONTINUOUS_CALLS + 1):
            phase = "首次调用" if index == 0 else f"连续调用 {index}/{CONTINUOUS_CALLS}"
            print(f"\n[{phase}] 调用中…", flush=True)
            timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
            status = "中断"
            started = time.perf_counter()
            try:
                response = await call_cloud_llm(prompt, settings=settings)
                status = "成功"
            except Exception as exc:
                status = f"失败：{type(exc).__name__}"
                raise
            finally:
                elapsed = time.perf_counter() - started
                log_file.write(
                    f"{timestamp} | 批次={batch} | {settings.llm_model_provider} | "
                    f"{endpoint} | {settings.llm_model} | {PROMPT_NAME} | {phase} | "
                    f"{status} | {elapsed:.3f} 秒\n"
                )
                log_file.flush()
                print(f"[{phase}] 耗时：{elapsed:.3f} 秒（{status}）", flush=True)

            durations.append(elapsed)
            print(f"模型回复：\n{response.text}", flush=True)

        continuous = durations[1:]
        summary = (
            f"首次调用：{durations[0]:.3f} 秒\n"
            f"连续调用（{len(continuous)} 次）：平均 {statistics.mean(continuous):.3f} 秒，"
            f"最快 {min(continuous):.3f} 秒，最慢 {max(continuous):.3f} 秒"
        )
        print(f"\n{summary}", flush=True)
        log_file.write(f"批次={batch} | {settings.llm_model} | {PROMPT_NAME} | 汇总\n{summary}\n\n")

    print(f"记录文件：{LOG_PATH}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
