"""M0 所有对用户可见的文案 —— 集中一处，改词只改这里。

依据：M0 网关方案 §2 / §11、`requirements.md` §7.1（T01 指令列表）/ §7.7（登记）/ §7.5。

指令列表按**路由表的 7 条**写（T01 截图原文是"5 条"，口径见 §8.1 待定义-37，按 7 条实现）。
"""

from __future__ import annotations

__all__ = [
    "COMMAND_LIST_TEXT",
    "FILE_RECEIVED",
    "FILE_MISSING",
    "IMAGE_REJECTED",
    "PARSING",
    "PARSE_FAILED",
    "EXTRACT_REJECTED",
    "DECOMPOSING",
    "NEEDS_RUBRIC",
    "NO_RUBRIC_FOUND",
    "VOTE_GENERATING",
    "VOTE_NEED_GROUP",
    "VOTE_NEED_ROSTER",
    "VOTE_IN_PROGRESS",
    "VOTE_CANDIDATES",
    "VOTE_ACK",
    "VOTE_BAD",
    "VOTE_TIMEOUT",
    "VOTE_SETTLED",
    "VOTE_SEAL_NEED_PICK",
    "VOTE_NEED_LEADER",
    "VOTE_GENERATE_FAILED",
    "COMPLETE_NEED_DM",
    "COMPLETE_NEED_ASSIGNMENTS",
    "COMPLETE_UNKNOWN",
    "COMPLETE_NOT_YOURS",
    "COMPLETE_MINE",
    "COMPLETE_MINE_NONE",
    "COMPLETE_OK",
    "COMPLETE_ALREADY",
    "REMIND_DUE",
    "REMIND_OVERDUE",
    "REPORT_NEED_GROUP",
    "REPORT_NEED_ROSTER",
    "REPORT_NEED_LEADER",
    "REPORT_NEED_ASSIGNMENTS",
    "REPORT_GENERATING",
    "REPORT_FAILED",
    "IMAGE_SEND_FAILED",
    "PREFERENCE_LIST",
    "PREFERENCE_SAVED",
    "PREFERENCE_BAD",
    "PREFERENCE_NEED_CARDS",
    "PREFERENCE_NEED_ROSTER",
    "PREFERENCE_NOT_MEMBER",
    "PREFERENCE_CONFIRM_SEAL",
    "PREFERENCE_DM_FAILED",
    "NEED_GROUP",
    "PROPOSAL_POSTED",
    "PROPOSAL_ACK",
    "PROPOSAL_EMPTY",
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
    "3. 方向 —— 群里发这句，我生成 2–3 个候选选题方向并开投票\n"
    "4. 你想做哪一块 —— 私聊我发这句，填志愿\n"
    "5. 我想提议：… —— 私聊我发这句，我匿名转达\n"
    "6. 完成 T3 —— 私聊我发这句，标记任务完成\n"
    "7. 登记 —— 群里发「登记」，按我回的表单 @ 人建花名册\n"
    "8. 报告 —— 组长在群里发这句，我把执行报告 + 甘特图发群"
)

# ---- 作业书 / 拆解 主链路 ----
FILE_RECEIVED = "已收到文件：{name}。回复「作业书」我来解析。"
FILE_MISSING = "请先把作业书文件发给我，再回复「作业书」。"
IMAGE_REJECTED = "图片我读不了，作业书请发 PDF / Word / TXT 文件。"
PARSING = "收到，正在解析作业书，大概需要半分钟…"
PARSE_FAILED = "解析失败，请检查文件是否是文字版；或回复「作业书」重试。"
EXTRACT_REJECTED = "这个文件没法用：{reason}"
DECOMPOSING = "收到，正在用现有评分点重新拆解…"
NEEDS_RUBRIC = "还没有评分点：先把作业书文件发给我，再回复「作业书」。"
NO_RUBRIC_FOUND = (
    "这份文件里我没找到评分标准（评分表 / 成绩评定 那一小节）。"
    "为了不瞎拆，我先停在这里 —— 请确认作业书里有没有评分标准，或换一份带评分标准的文件。"
)

# ---- M2 方向候选 + 群内投票（§7.1 / §7.6 / D-35 / D-36）----
VOTE_GENERATING = "收到，正在按评分点生成候选方向，大概需要半分钟…"
VOTE_NEED_GROUP = "投票是群里的动作：请到群里发「方向」。"
VOTE_NEED_ROSTER = "还没有花名册：先在群里发「登记」建一份，再发「方向」。"
VOTE_IN_PROGRESS = "方向投票正在进行：还剩 {minutes} 分钟，回复数字投票就行。"
# 候选文案里这句"仅供参考，由全组拍板"是 §7 验收项，别删。
VOTE_CANDIDATES = (
    "候选方向（仅供参考，由全组拍板）：\n"
    "{items}\n\n"
    "回复数字投票（一人一票，可以改）；10 分钟内过半就定。"
)
VOTE_ACK = "记下了：你投的是 {id}. {title}。想改就再发一次数字。"
VOTE_BAD = "没看懂：候选只有 {ids}，回复其中一个数字就行。"
# 超时后窗口只是**冻住**（vote.closed），候选与票数都还在，所以直接让组长拍板就行 ——
# 别再让人重开一轮：重开会重新生成候选，编号跟这张票数表就对不上了。
VOTE_TIMEOUT = (
    "10 分钟到，还没有方向过半：{tally}。数字不再计票。\n"
    "组长拍板：发「封盘」取票最多的，或「封盘 2」直接指定第 2 个。"
)
VOTE_SETTLED = "方向已定：{id}. {title}（{detail}）。"
VOTE_SEAL_NEED_PICK = "现在还没有票：组长发「封盘 2」直接指定一个方向（数字是候选编号）。"
VOTE_NEED_LEADER = "只有组长能封盘。"
VOTE_GENERATE_FAILED = "候选方向没生成出来（模型输出不合要求），稍后再发一次「方向」。"

# ---- M6 完成标记（§7.1 第 6 条 / D-22 / D-31）----
COMPLETE_NEED_DM = "这条要私聊我发：私聊发「完成 T3」我就给你标上。"
COMPLETE_NEED_ASSIGNMENTS = "还没有分配：先在群里发「你想做哪一块」，拿到的卡才能标完成。"
# 不是你的卡 / 没这张卡时，都**列出他自己领到的卡** —— 不许给假确认（§6.3 的口径）
COMPLETE_UNKNOWN = "没有 T{index} 这张任务卡。{mine}"
COMPLETE_NOT_YOURS = "T{index} 不是我分给你的卡，我不能替你标。{mine}"
COMPLETE_MINE = "你手上的是：{tasks}。"
COMPLETE_MINE_NONE = "你手上现在没有任务卡。"
COMPLETE_OK = "已标记完成：{task_id}（{module}）。"
COMPLETE_ALREADY = "{task_id} 之前就标过了（{at}），我没改时间。"

# ---- M6 临期催办（§2.2；两档 = 待定义-35，逾期档 = D-66）----
# ``{at}`` 是飞书的 @ 语法 ``<at user_id="ou_x"></at>``，写成纯文本 @某人 不会真 @。
REMIND_DUE = (
    "{at} 你的「{module}」还差 {hours} 小时到截止（{deadline}），"
    "做完私聊我发「完成 {task_id}」。"
)
REMIND_OVERDUE = (
    "{at} 你的「{module}」已经逾期了（截止 {deadline}），"
    "做完私聊我发「完成 {task_id}」。"
)

# ---- M7 执行报告（D-64）----
REPORT_NEED_GROUP = "报告是群里的动作：请到群里发「报告」。"
REPORT_NEED_ROSTER = "还没有花名册：先在群里发「登记」建一份，再发「报告」。"
REPORT_NEED_LEADER = "只有组长能要报告。"
REPORT_NEED_ASSIGNMENTS = "还没有分配：先在群里发「你想做哪一块」，分配完再发「报告」。"
REPORT_GENERATING = "收到，正在生成执行报告（总表 + 核对清单 + 甘特图），马上发群…"
REPORT_FAILED = "执行报告没生成出来（渲染出错），稍后再发一次「报告」。"
IMAGE_SEND_FAILED = "甘特图没发出去（网络问题），上面的文字版先到；稍后再发一次「报告」我补一张。"

# ---- M4 志愿分配（§7.1 / D-52~D-54）----
PREFERENCE_LIST = (
    "任务卡清单（回复序号即可，想排序就按优先级发，例如「2 1」）：\n"
    "{items}"
)
PREFERENCE_SAVED = "记下了：你的志愿是 {tasks}。想改就再发一次序号。"
PREFERENCE_BAD = "序号我没看懂。我看到的是 {tasks}，重发一次序号就行。"
PREFERENCE_NEED_CARDS = "还没有任务卡：先把作业书文件发给我，回「作业书」拆出任务卡。"
PREFERENCE_NEED_ROSTER = "还没有花名册：先在群里发「登记」建一份，再发「你想做哪一块」。"
PREFERENCE_NOT_MEMBER = "我这份花名册里没有你：先在群里「登记」把你自己 @ 进去，再私聊我填志愿。"
# 组长重发「你想做哪一块」不再直接封盘（P0-B / D-56）：有人交过就先确认一次，
# 免得"为了再发一遍清单"顺手把窗口关了、不可撤回。
PREFERENCE_CONFIRM_SEAL = (
    "现在封盘会按已有 {done} 份志愿分配，还有 {missing} 人没交。"
    "回复「封盘」确认，回复别的继续等。"
)
# 主动私聊发不出去时，在群里把话说清楚（P0-C），别"群里说已发、实际没人收到"。
PREFERENCE_DM_FAILED = (
    "有 {count} 人我没能私聊到。请这几位私聊我发「你想做哪一块」，我单独回你清单。"
)

# 主动发群 / 私聊的前置：机器人得先见过至少一条群消息，才知道"群"是哪个（D-54）。
NEED_GROUP = "我还没认下群：先在群里发一次指令（例如「作业书」），我认一下群。"

# ---- M5 匿名代言（§6.5 / D-55）----
PROPOSAL_POSTED = "有组员提议：{text}"
PROPOSAL_ACK = "已经匿名发到群里了。"
PROPOSAL_EMPTY = "「我想提议：」后面要写上内容，例如「我想提议：前端用 React」。"

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
