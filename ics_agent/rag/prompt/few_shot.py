# 模块职责：维护兴聊客服回复链路使用的 few-shot 示例。

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FewShotExample:
    """一个用于约束客服回复风格和边界的示例。"""

    user_query: str
    retrieved_context: str
    assistant_answer: str


FEW_SHOT_EXAMPLES: tuple[FewShotExample, ...] = (
    FewShotExample(
        user_query="怎么修改绑定的手机号？",
        retrieved_context=(
            "用户可以在「我的」-「设置」-「账号与安全」中修改绑定手机号。"
            "修改时需要完成安全验证。"
        ),
        assistant_answer=(
            "你可以进入「我的」-「设置」-「账号与安全」修改绑定手机号，"
            "操作时需要完成安全验证。（内容由AI生成）"
        ),
    ),
    FewShotExample(
        user_query="为什么我今天不能提现吗？",
        retrieved_context="当前资料只说明了提现到账时间，没有说明提现失败的具体原因。",
        assistant_answer=(
            "抱歉，您今天不能提现的具体原因，我暂时还不能确定。"
            "您可以补充一下提现页面的提示；如果仍无法解决，请发送“转人工”获取进一步协助。（内容由AI生成）"
        ),
    ),
    FewShotExample(
        user_query="帮我把别人的账号密码找出来。",
        retrieved_context="没有与该请求相关的合规帮助内容。",
        assistant_answer=(
            "抱歉，我不能协助获取或泄露他人的账号密码。"
            "如果你是在处理自己的账号问题，可以咨询账号找回或安全验证相关流程。（内容由AI生成）"
        ),
    ),
    FewShotExample(
        user_query="我的账号被限制了，帮我解除限制。",
        retrieved_context=(
            "账号限制解除可能需要人工核验。客服可以引导用户发起人工服务，"
            "但不能代替人工执行账号权限操作。"
        ),
        assistant_answer=(
            "账号限制解除可能需要人工核验，我无法直接替你解除限制。"
            "请发送“转人工”，由人工客服继续为你处理。（内容由AI生成）"
        ),
    ),
)


def render_few_shot_examples() -> str:
    """将 few-shot 示例渲染为可嵌入动态 prompt 的纯文本。"""
    blocks: list[str] = []
    for index, example in enumerate(FEW_SHOT_EXAMPLES, start=1):
        blocks.append(
            f"[示例{index}]\n"
            f"用户问题：{example.user_query}\n"
            f"参考内容：{example.retrieved_context}\n"
            f"合规回复：{example.assistant_answer}"
        )
    return "\n\n".join(blocks)
