# Agent Workboard MVP-LITE-v1 操作指南

## 唯一入口

用户只向编排 Agent 下指令。编排 Agent 创建或定位一个顶层 WorkItem，并按当前状态派发规划、实施或复审角色。内部 Txx 只是 WorkItem 内任务，不创建顶层卡片。

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

## 搁置、阻塞和认领

- 每个非终态都允许进入 `HELD`；搁置项不得被自动认领。
- 外部依赖未满足时使用 `BLOCKED` 并写明原因。
- 一个 WorkItem 同时最多一个活动 claim；同一仓库同时最多一个活动写锁。
- 租约过期后可由编排器回收并以更大的 generation 重新认领。

## 提交契约

`submit_plan` 和 `submit_implementation` 都要求提交者持有活动 claim；二者在成功 transition 内原子释放该 claim。若存在活动 repository writer lock，必须先释放该 lock。不得先 release WorkItem claim 再提交。

## 明确不纳入默认路线图

RunnerAuthority、逐操作 lease、进程静止证明、Git commit/tree 加密绑定、恶意 SQLite 篡改防御和分布式投影恢复不属于第一期，也不默认进入第二期；只有真实故障或明确业务要求证明必要时，才另立 WorkItem。第二期仅把同一套简单模型换成 MySQL，并增加经过身份认证的远程下指令、暂停、恢复和 steer。
