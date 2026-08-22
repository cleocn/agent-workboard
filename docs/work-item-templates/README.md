# WorkItem 类型模板入口与统一执行契约

> Management Contract Version：`AWB-WORKITEM-MGMT-v1`  
> Template Set Version：`AWB-WORKITEM-TEMPLATES-v1`

本目录是 Agent Workboard 项目正式、持续维护的 WorkItem 创建与规划输入入口，适用于当前五种顶层类型 `TI / FE / R / WA / AWB`。模板帮助人和 Agent 收集 scope、授权、任务、验收及证据；WorkItem 一旦创建，SQLite 中由 `work_items + tasks + events + reviews` 投影出的 management 视图是活动 WorkItem 的单一运行时权威。

模板契约版本：`AWB-MANAGEMENT-v1`。该值必须与 management envelope、schema、runtime validator/projection、workflow、Agent guide 和测试一致。

禁止把复制出的 Markdown 当作第二套运行状态并手工同步 state、queue、task status、progress、current task 或 next step。需要展示时应由 `show/timeline` 投影或导出；scope、授权、验收和任务板的有效变更必须通过受控 API/CLI 追加事件。模板不得覆盖数据库事实。

## 唯一路由

| WorkItem 类型 | 创建与规划模板 | 附加入口规则 |
| --- | --- | --- |
| TI | [`test-issue-template.md`](test-issue-template.md) | 必须同时应用 [`test-issue-trigger-rules.md`](test-issue-trigger-rules.md) |
| FE | [`fe-template.md`](fe-template.md) | 无第二模板 |
| R | [`remediation-plan-template.md`](remediation-plan-template.md) | 无第二模板 |
| WA | [`wa-template.md`](wa-template.md) | 无第二模板 |
| AWB | [`awb-template.md`](awb-template.md) | 无第二模板 |

一个类型只有上述唯一入口；不得复制旧工作区路径、编号规则或状态机作为并行规范。

`usage-observation-template.md` 是 AWB-009 最终验收后才可使用的长期观察附加启动包；它不替代 `awb-template.md`，也不构成第六种顶层类型或并行运行时权威。

## 统一 management 输入

所有新 WorkItem 首次创建前，模板输入必须足以生成版本化 management envelope：

- `workItemId/type/title/mode/priority/createdAt/updatedAt`；
- 顶层 `state/queueState`、progress、current task、唯一 next step；
- `scope/outOfScope/authorization/safetyConstraints`；
- 使用 `<WI-ID>-T01...` 的连续编号任务板，含 owner、required、task status、acceptance 和 closure evidence；
- WorkItem acceptance、closure、证据引用和初始状态历史。

顶层 lifecycle state 与 queue_state 属于 WorkItem 层；任务只使用 `NOT_STARTED / IN_PROGRESS / BLOCKED / WAITING_ACCEPTANCE / COMPLETED / CANCELLED`。同一 WorkItem 最多一个 `IN_PROGRESS`。默认进度为“已完成 required tasks / required tasks 总数”，required `CANCELLED` 不算完成且阻止关闭。

旧模板中的 `T00` 是历史展示格式。新建或重新补齐到当前运行时的任务必须从 `<WI-ID>-T01` 连续编号；不得把旧 `T00` 直接持久化。模板中的 emoji 只用于展示，必须一一映射到六种 task status，不是额外状态。

## 类型语义边界

- TI 的“待处理 / 暂不处理 / 已解决”是问题处置语义，不得替代 WorkItem lifecycle、queue 或 task status；“已解决”仍受统一 closure gate 约束。
- FE 若保留冻结阶段权重，management envelope 必须显式使用 `progressMethod=WEIGHTED` 并保存每项稳定 ID/权重，权重总和必须为 100；创建后只能经有授权的 management amendment 修改。未满足这些条件时使用默认 required-task 公式。
- R 的视觉复审仅在 scope 涉及 UI 时作为条件 acceptance evidence；它不增加系统 Reviewer、人工审批层或并行 gate。
- WA 的“迁移阶段”是从任务/验收证据派生的类型业务阶段，只用于展示与业务判断，不得替代 WorkItem lifecycle 或 task status，也不得单独驱动 transition。
- AWB 使用统一模型管理 Workboard 自身的协议、运行时、迁移兼容和回归门禁；不得把规划、实施或复审拆成替代顶层 WorkItem 来绕过轮次或授权。

模板中出现 commit、push、merge、deploy、publish、真实写入、远程服务、真实回调或环境操作时，都只能写为“在当前 WorkItem 明确授权后适用”的条件任务/验收证据。模板本身不授予权限；没有授权必须记录“未授权/未执行”，不能阻止与其无关的合法关闭，也不能把动作变成默认要求。

## 单一运行时权威与历史

新建 WorkItem 的 envelope、任务和 `WORK_ITEM_CREATED` 必须在同一事务落盘。历史 WorkItem 不批量改写；重新打开、恢复实质工作或 scope/授权/验收扩项前，通过受控 backfill 追加完整 envelope。后续变更只能追加 management amendment 和状态事件，`show/timeline` 从 SQLite 投影当前事实与历史。

Markdown 模板可以保留示例、问题域表格和规划提示，但不得要求人手持续维护一份与 SQLite 平行的实时状态。若需要静态交付文档，必须标明生成时间与来源，且数据库投影优先。

## 同批同步门禁

本目录八个正式文件是一个受版本约束的模板集合：本 README、五种类型模板、TI trigger rules 和 usage observation 附加启动包。任何模板字段、状态语义、进度算法、任务编号、授权或关闭规则变更，必须在同一批变更中同步并验证：

1. `workitem.schema.json` management 契约；
2. `lite.py` create/show/timeline/task/transition/review/gate 的运行时读取、投影与校验；
3. `workflow.yaml` 与 `AGENT-GUIDE.md`；
4. Planner、Implementer、Reviewer、Orchestrator 指令；
5. 模板路由、必备章节、版本一致性和主路径测试。

只修改 Markdown、但运行时不读取或不校验的“规范更新”不完整，不得合并为已完成。当前模板集合已最小对齐 T01+、六种 task status 的展示映射、FE 权重、TI/WA/R 类型语义、条件 evidence 和授权措辞；领域表格继续保留。对应 runtime、schema、workflow、Agent guide、Agent 指令和确定性测试必须保持同批一致。
