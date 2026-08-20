# Agent Workboard MVP-LITE-v1 操作指南

## 唯一入口

用户只向编排 Agent 下指令。编排 Agent 创建或定位一个顶层 WorkItem，并按当前状态派发规划、实施或复审角色。内部 Txx 只是 WorkItem 内任务，不创建顶层卡片。

`docs/work-item-templates/README.md` 是 TI、FE、R、WA、AWB 的正式模板路由。新建记录必须在 create 时提供 `AWB-WORKITEM-MGMT-v1`；envelope、T01+ tasks 与创建事件在同一事务写入。历史记录可只读，但重新打开、恢复实质工作或扩项前必须通过 `management-backfill` 补齐。活动状态只以 SQLite `show/timeline` 投影为准。

## 角色操作

- `ORCHESTRATOR`：创建 WorkItem、选择下一角色、维护优先级和搁置；不代替人工批准。
- `PLANNER`：读取范围、形成复现/根因/方案或实施计划，完成后执行 `submit_plan`。
- `IMPLEMENTER`：只在“规划复审通过”后实施；完成全部实施任务和本地测试后执行 `submit_implementation`。
- `REVIEWER`：独立复审整份规划或整批实施结果；不对每个子任务逐一复审，不修改被审内容。

## Agent 每次运行的固定顺序

1. 读取 WorkItem、当前任务、授权边界、验收标准和停止点。
2. 仅当队列为 `CLAIMABLE` 时认领；记录 `agent_id`、角色、租约和 generation。
3. 写仓库前取得仓库单写锁；只读操作不取得写锁。
4. 执行任务并记录简短证据引用。不得写入密码、Token 或隐藏思维链。
5. 持有 WorkItem claim 完成当前任务；先释放仓库 writer lock，再提交规划或实施结果。提交 transition 成功时原子释放 claim，提前 release claim 的提交必须被拒绝。
6. 达到用户指定停止点、人工门禁、`HELD` 或 `BLOCKED` 时停止。

## 复审与退回

- 规划 Agent 复审通过后，STANDARD 等待人工规划批准；驳回返回 `DRAFT`。
- 最终 Agent 复审通过后，STANDARD 等待人工最终验收；驳回返回 `IMPLEMENTING`。
- `READ_ONLY_DIAGNOSIS` 只创建规划和规划复审任务，可按指令停在规划提交或 Agent 规划复审，不进入实施。
- 实施采用批量复审：全部实施任务和本地测试完成后只进行一次最终 Agent 复审。

每个阻断 Finding 必须有唯一 ID、PLAN/IMPLEMENTATION stage、被违反的明确契约、可核验证据、具体影响和最小关闭条件。缺项意见只能是建议；只有建议的复审必须 PASS。第 2 轮起只检查开放 Finding、本轮直接回归或此前客观不可得的新证据；新 Finding 必须说明来源和此前不可得原因。

PLAN 与 IMPLEMENTATION 独立使用严格串行 `3+1+1`。第 1～3 轮使用普通 Reviewer；第 3 轮仍 REVISE 时冻结 artifact，不返回作者，直接调用唯一一次第 4 轮高级 convergence reviewer。其结果只允许 PASS、CONVERGENCE_REVISE、WAITING_HUMAN 或真实 BLOCKED；只有 CONVERGENCE_REVISE 允许一次最小返工。第 5 轮由普通 Reviewer 仅核验高级关闭条件和直接回归，仍 REVISE 就进入 WAITING_HUMAN，禁止第 6 轮。Reviewer、claim generation、重复提交、人工退回或 PLAN_DEVIATION 都不重置累计轮次。

## 搁置、阻塞和认领

- 每个非终态都允许进入 `HELD`；搁置项不得被自动认领。
- 外部依赖未满足时使用 `BLOCKED` 并写明原因。
- 一个 WorkItem 同时最多一个活动 claim；同一仓库同时最多一个活动写锁。
- 租约过期后可由编排器回收并以更大的 generation 重新认领。

## 提交契约

`submit_plan` 和 `submit_implementation` 都要求提交者持有活动 claim；二者在成功 transition 内原子释放该 claim。若存在活动 repository writer lock，必须先释放该 lock。不得先 release WorkItem claim 再提交。

每次实施提交还必须记录已通过 acceptance、测试命令/结果、实际修改范围、已知非阻断问题、关闭 Finding、复杂度变化和回归。删测、跳测、放宽断言、已有验收倒退或新同级缺陷会停止自动提交。修复需要未批准模块、契约、数据结构、基础设施或显著扩项时使用 `plan-deviation`，实现 task 进入 BLOCKED、回到 Planner，既有复审轮次不重置。

任务只能沿已定义边推进，同一 WorkItem 最多一个 IN_PROGRESS；BLOCKED、WAITING_ACCEPTANCE、COMPLETED 或 CANCELLED 必须有证据。`show` 与 HTTP detail 投影 management、任务板、进度、currentTask、唯一 nextStep 和两阶段 review counters；board/list 只显示进度与 nextStep 摘要。

## 明确不纳入默认路线图

RunnerAuthority、逐操作 lease、进程静止证明、Git commit/tree 加密绑定、恶意 SQLite 篡改防御和分布式投影恢复不属于第一期，也不默认进入第二期；只有真实故障或明确业务要求证明必要时，才另立 WorkItem。第二期仅把同一套简单模型换成 MySQL，并增加经过身份认证的远程下指令、暂停、恢复和 steer。
