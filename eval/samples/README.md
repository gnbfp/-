# eval/samples —— 合成演示样本

> ⚠️ **本目录只有合成样本，不是真实作业书。**
> 真实作业书按定稿 §3.9 收集（目标 15 份、先 5 份供人工基线），**原文不入库**（理由见 `requirements.md` §9 的 D-41）。

## 文件

| 文件 | 说明 |
|---|---|
| `assignment_demo.pdf` | 合成作业书：链表通讯录（40 分）+ 设计报告（30 分，措辞模糊）+ 现场答辩（30 分） |
| `make_demo_assignment.py` | 用 PyMuPDF 现场生成上面那份 PDF（含一张带框线的评分表） |

## 用途

让 D5 门②「主链路命令行跑通」有**可复现的输入**，队友不用只听口头结论：

```bash
python eval/samples/make_demo_assignment.py
python -m src.main --file eval/samples/assignment_demo.pdf
```

预期：M1 抽出 3 个评分点（"设计报告"因"内容充实、排版美观"标 `ambiguous`），
M3 出任务卡并跑自检循环，最后打印「评分点核对清单 + 覆盖率」。
LLM 输出有正常波动（实测同一输入会出 5–6 张卡），覆盖率与均衡度可能随之变化。

## 边界

- 它**只证明管线通**，不能当覆盖率证据——覆盖率数字要等真实作业书。
- `eval/` 不得 import `src/`（B3 / B8）；本目录的生成脚本同理，只依赖 PyMuPDF。
