# vision-worker 提示词（执行卡 7.1）

- 文件名 `<名称>-v<版本>.txt`，版本只增不改：改一个字就新开 `v2`，旧文件保留，线索里的 `model_version` 写 `<模型名>@<名称>-v<N>`。
- 提示词只要求模型输出有限枚举和「指回输入帧 / 区域」的引用；结论（违规与否）由规则与人工复核作出，模型不出结论。
- 改提示词前必须有评测集与对照（`ly/docs/03-步骤/修改提示词.md`）；本轮没有评测集，不得改 `visual-review-v1.txt` 正文。
- 登记：`ly/docs/11-台账/提示词登记表.md`（PR-01，建议待用户确认）。

| 名称 | 版本 | 用途 | 输出 |
|---|---|---|---|
| visual-review | v1 | 候选物品的多帧多模态复核（S01 / S02 / S08 / S09 的可见事实） | `object_kind / visibility / holding / activity / evidence / notes` |
| core-interactions | v2 | 连续画面的近距交流、资料递接、手机通话/拍摄事实；输入轨迹与帧引用校验 | `facts[kind / track_ids / visibility / evidence_frames / object_visible / confidence]`，拍摄还需 `target_visible` |

core-interactions-v2 的评测入口为 `tests/test_core_behaviors.py` 与项目根 `scripts/verify_visual_model.py`；代码中记录提示词 SHA256，不能由模型填入身份、授权或时间。
