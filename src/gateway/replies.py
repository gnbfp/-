"""M0 所有对用户可见的文案 —— 集中一处，改词只改这里。

依据：M0 网关方案 §2 / §11、`requirements.md` §7.1（T01 指令列表）/ §7.7（登记）/ §7.5。

指令列表按**路由表的 7 条**写（T01 截图原文是"5 条"，口径见 §8.1 待定义-37，按 7 条实现）。
"""

from __future__ import annotations

__all__ = [
    "COMMAND_LIST_TEXT",
    "FILE_RECEIVED",
    "FILE_MISSING",
    "PARSING",
    "PARSE_FAILED",
    "EXTRACT_REJECTED",
    "DECOMPOSING",
    "NEEDS_RUBRIC",
    "PLACEHOLDER_DIRECTION",
    "PLACEHOLDER_VOTE",
    "PLACEHOLDER_PREFERENCE",
    "PLACEHOLDER_PROPOSAL",
    "PLACEHOLDER_COMPLETE",
    "REGISTER_FORM",
    "REGISTER_FORM_BAD",
    "REGISTER_NEED_LEADER",
    "REGISTER_NEED_MEMBERS",
    "REGISTER_CONFIRM",
    "REGISTER_SAVED",
    "REGISTER_CANCELLED",
    "REGISTER_EXPIRED",
]

COMMAND_LIST_TEXT = (
    "我只会这几件事：\n"
    "1. 作业书 —— 先发作业书文件，再回复「作业书」，我抽评分点并拆任务卡\n"
    "2. 拆解 —— 用现有评分点重跑一次任务拆解\n"
    "3. 方向 —— 生成 2–3 个候选选题方向（开发中）\n"
    "4. 你想做哪一块 —— 私聊我发这句，填志愿（开发中）\n"
    "5. 我想提议：… —— 私聊我发这句，我匿名转达（开发中）\n"
    "6. 完成 T3 —— 私聊我发这句，标记任务完成（开发中）\n"
    "7. 登记 —— 群里发「登记」，按我回的表单 @ 人建花名册"
)

# ---- 作业书 / 拆解 主链路 ----
FILE_RECEIVED = "已收到文件：{name}。回复「作业书」我来解析。"
FILE_MISSING = "请先把作业书文件发给我，再回复「作业书」。"
PARSING = "收到，正在解析作业书，大概需要半分钟…"
PARSE_FAILED = "解析失败，请检查文件是否是文字版；或回复「作业书」重试。"
EXTRACT_REJECTED = "这个文件没法用：{reason}"
DECOMPOSING = "收到，正在用现有评分点重新拆解…"
NEEDS_RUBRIC = "还没有评分点：先把作业书文件发给我，再回复「作业书」。"

# ---- 未接上的模块：只留入口，业务逻辑归各自 owner（方案 §12）----
PLACEHOLDER_DIRECTION = "「方向」还没接上候选方向生成（M2 在 9/16 接）。现在可以先用「作业书」把任务卡拆出来。"
PLACEHOLDER_VOTE = "投票还没接上（M2 在 9/16 接）：现在回复数字我还没法计票。"
PLACEHOLDER_PREFERENCE = "「你想做哪一块」还没接志愿分配（M4 在 9/16 接）。"
PLACEHOLDER_PROPOSAL = "「我想提议」还没接匿名代言（M5 在 9/16 接）。"
PLACEHOLDER_COMPLETE = "「完成 T3」还没接完成标记（M6 在 9/16 接）。"

# ---- 登记（§7.7）----
REGISTER_FORM = (
    "请照这个样子填，@到每个人：\n"
    "登记\n"
    "组长：@某人\n"
    "组员：@某人 @某人 @某人"
)
REGISTER_FORM_BAD = (
    "表单没看懂。请照下面这个格式重发一遍（**必须用 @ 人**，不要打名字）：\n\n" + REGISTER_FORM
)
REGISTER_NEED_LEADER = "表单里「组长」要正好 1 个人，请改完重发一次。"
REGISTER_NEED_MEMBERS = "表单里「组员」至少 2 个人，请改完重发一次。"
REGISTER_CONFIRM = (
    "我读到的是：\n组长：{leader}\n组员：{members}（含组长共 {total} 人）\n\n"
    "回复「同意」保存，回复别的就作废。"
)
REGISTER_SAVED = "花名册已保存：组长 {leader}，含组长共 {total} 人。"
REGISTER_CANCELLED = "已作废，原有名单没动。"
REGISTER_EXPIRED = "登记超时作废了，原有名单没动。要登记请再发一次「登记」。"
