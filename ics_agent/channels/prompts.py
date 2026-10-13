"""读取当前渠道的提示词；文件调整后下一轮回复即可使用。"""

from .config import PROJECT_ROOT, get_channel


def get_system_prompt(channel_key: str | None = None) -> str:
    channel = get_channel(channel_key)
    path = PROJECT_ROOT / channel.prompt_path
    prompt = path.read_text(encoding="utf-8").strip()
    if not prompt:
        raise ValueError(f"Channel prompt is empty: {channel.prompt_name}")
    return prompt + "\n\n"
