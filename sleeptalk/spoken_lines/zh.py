"""Chinese (zh). Simplified characters, Mandarin, everyday spoken register.

Replies split into sentences on ``。！？`` and flush a long opening at
``，；：`` (sleeptalk/text.py). Written by Claude Fable 5.1 on 2026-09-17;
not yet checked by a native speaker.
"""

PREAMBLE_POOLS = {
    "ack": ("好的。", "行。", "明白。", "嗯。", "对。", "好。"),
    "action": (
        "我看一下。", "我查一下。", "我来看看。",
        "现在查。", "我找一下。", "稍等，我查一下。",
    ),
    "ack_action": (
        "好的，我看一下。", "明白，我查一下。",
        "行，我来看看。", "好，我查查。",
    ),
    "action:task": ("好，我来。", "明白，马上做。", "现在就做。", "在做了。"),
    "ack:empathy": ("哦，我明白了。", "啊，好的。", "嗯，我理解。"),
    "working": ("还在做。", "还在处理。"),
    "control": ("停了。", "好，停了。"),
    "greeting": ("嗯，我在。", "你好你好。", "哈喽。"),
    "cron_offer": (
        "欢迎回来。要我看看你定时任务的最新结果吗？",
    ),
    "reassure": (
        "还在处理中。",
        "稍等，还在进行。",
        "还没好，快了。",
        "快好了，稍等一下。",
        "有点慢，但我在做。",
        "没忘了你，还在做。",
        "还在查。",
        "请稍等一下。",
        "这个还需要一点时间。",
        "再等一下，还在查。",
    ),
}

REASSURE_TOOL_POOLS = {
    "read": ("还在读。", "还在看这个文件。", "还在翻这个文件。"),
    "edit": ("还在改。", "修改还在进行。", "还在处理这些改动。"),
    "write": ("还在写。", "还在写文件。", "文件还在写入。"),
    "bash": (
        "那条命令还在跑。", "还在运行命令。",
        "命令还没结束。", "那条命令有点慢。",
    ),
    "search": ("还在搜。", "还在查。", "搜索有点慢。", "还在拉取结果。"),
    "other": ("还在处理中。", "稍等，还在进行。", "还在做，稍等一下。"),
}

NARRATION_PHRASES = {
    "read": "在读文件。",
    "edit": "在改代码。",
    "write": "在写文件。",
    "bash": "在运行命令。",
    "search": "在搜索。",
    "task": "在启动一个子任务。",
    "mcp": "在用我的一个工具。",
    "other": "在处理。",
}
