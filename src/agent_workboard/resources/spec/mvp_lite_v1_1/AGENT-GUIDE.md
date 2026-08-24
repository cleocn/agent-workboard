# Agent Workboard MVP-LITE-v1 操作指南

## 唯一入口

用户只向编排 Agent 下指令。编排 Agent 创建或定位一个顶层 WorkItem，并按当前状态派发规划、实施或复审角色。内部 Txx 只是 WorkItem 内任务，不创建顶层卡片。

`docs/work-item-templates/README.md` 是 TI、FE、R、WA、AWB 的正式模板路由。新建记录必须在 create 时提供 `AWB-WORKITEM-MGMT-v1`；envelope、T01+ tasks 与创建事件在同一事务写入。历史记录可只读，但重新打开、恢复实质工作或扩项前必须通过 `management-backfill` 补齐。活动状态只以 SQLite `show/timeline` 投影为准。

## 角色操作

- `ORCHESTRATOR`：创建 WorkItem、选择下一角色、维护优先级和搁置；只能按持久化策略消费 SYSTEM 自动门，不能冒充 HUMAN。
- `PLANNER`：读取范围、形成复现/根因/方案或实施计划，完成后执行 `submit_plan`。
- `IMPLEMENTER`：只在“规划复审通过”后实施；完成全部实施任务和本地测试后执行 `submit_implementation`。
- `REVIEWER`：独立复审整份规划或整批实施结果；不对每个子任务逐一复审。Implementation Reviewer 始终只读；opt-in PLAN Reviewer 只能通过 package-owned amendment 命令修正获批的非实质问题。

## Agent 每次运行的固定顺序

1. 读取 WorkItem、当前任务、授权边界、验收标准和停止点。
2. 仅当队列为 `CLAIMABLE` 时认领；记录 `agent_id`、角色、租约和 generation。
3. 写仓库前取得仓库单写锁；只读操作不取得写锁。
4. 执行任务并记录简短证据引用。不得写入密码、Token 或隐藏思维链。
5. 持有 WorkItem claim 完成当前任务；先释放仓库 writer lock，再提交规划或实施结果。提交 transition 成功时原子释放 claim，提前 release claim 的提交必须被拒绝。
6. 达到用户指定停止点、人工门禁、`HELD` 或 `BLOCKED` 时停止。只有项目 `usagePolicy=BEST_EFFORT` 才在状态变化后同步脱敏 usage；缺失或 `OFF` 时完全跳过。

## 创建风险与 gate 策略

每次 create 前，主 Agent 必须把实际 scope、获准动作和已观测状态分类为
`AWB-CREATION-RISK-v1`。实际远程写入为 `REMOTE`，删除/覆盖/reset/难恢复变更为
`DESTRUCTIVE`，身份或 schema 漂移、冲突 lease/writer、失败预检或无法解释状态为
`ANOMALOUS_STATE`。`outOfScope` 和 `authorization.forbidden` 中的否定声明不单独触发。

普通新建 STANDARD WorkItem 默认 `AUTO_ON_PASS`；创建者明确关闭自动通过时使用
`MANUAL`。风险信号非空时，未取得用户对 `AUTO_ON_PASS / MANUAL` 的明确选择不得
调用 create。选择、决策人和脱敏原因进入创建审计；选择 AUTO 只改变 workflow gate，
绝不授权远程、发布、部署、删除或破坏性动作。旧 WorkItem 和省略新字段的旧 transfer
bundle 迁移为 `MANUAL`。

## 复审与退回

- 规划 Agent 复审 PASS/open0 后，STANDARD 的 `AUTO_ON_PASS` 在同一事务写
  `SYSTEM/AUTO_GATE_APPROVED` 并进入 `PLAN_REVIEW_APPROVED`；`MANUAL` 等待人工规划批准。
- 最终 Agent 复审 PASS/open0 且全部 required task、测试与质量基线通过后，
  `AUTO_ON_PASS` 自动进入终态；`MANUAL` 等待人工最终验收。驳回返回 `IMPLEMENTING`。
- REVISE、BLOCKED、WAITING_HUMAN、PLAN_DEVIATION、开放 Finding、测试失败或状态漂移
  一律 fail closed；自动事件不写 `human_gates`，也不产生 HUMAN actor。
- `READ_ONLY_DIAGNOSIS` 只创建规划和规划复审任务，可按指令停在规划提交或 Agent 规划复审，不进入实施。
- 实施采用批量复审：全部实施任务和本地测试完成后只进行一次最终 Agent 复审。

每个阻断 Finding 必须有唯一 ID、PLAN/IMPLEMENTATION stage、被违反的明确契约、可核验证据、具体影响和最小关闭条件。缺项意见只能是建议；只有建议的复审必须 PASS。第 2 轮起只检查开放 Finding、本轮直接回归或此前客观不可得的新证据；新 Finding 必须说明来源和此前不可得原因。

PLAN 与 IMPLEMENTATION 独立使用严格串行 `3+1+1`。新 PLAN 以 `--plan-artifact` 显式 opt in；每轮使用未参加过该 PLAN 的 Reviewer，latest editor 不得自审。R1～R3 可 PASS、以 package-owned `--replacement-file` 原子 AMENDED，或把实质变更 REVISE_TO_PLANNER；R3 两种修改结果都直接进唯一 R4 convergence。R4 只允许一次最小 AMENDED 后进入 fresh ordinary R5；R5 只允许 PASS/WAITING_HUMAN，禁止修改和第 6 轮。Reviewer 不直接写文件，PASS 不取得 writer；旧 PLAN 保持 AWB-REVIEW-v1 只读语义。IMPLEMENTATION 的 REVISE/CONVERGENCE_REVISE、Reviewer 禁止编辑产品和全部质量门保持不变。Reviewer、claim generation、人工退回或 PLAN_DEVIATION 都不重置累计轮次。

## 孤立 Reviewer 任务恢复

PLAN Reviewer 只领取 Reviewer task 并提交 review，禁止为 PLAN review 手工把复用的
Reviewer task 改为 `IN_PROGRESS`；该 task 留给 FINAL review 完成。若旧运行序列已在 PLAN
PASS 后遗留一个无 claim 的 Reviewer `IN_PROGRESS` task，只有 HUMAN 可调用：

```text
awb lite --project <project> recover-review-task <WorkItem> <Task> \
  --human <human-id> --reason <reason> --request-id <unique-id>
```

`AWB-REVIEW-TASK-RECOVERY-v1` 不是通用 reset。它只接受唯一 Reviewer task、唯一
`IN_PROGRESS`、无活动 claim/writer、已释放且身份匹配的 Reviewer claim、structured PLAN
PASS/open0、对应 PLAN review event、已通过 gate、后续 `START_IMPLEMENTATION`、无 FINAL
review，以及精确 `IMPLEMENTING` 或 `PLAN_DEVIATION` 投影。成功只把该 task 恢复为
`NOT_STARTED`、递增 WorkItem row version 并追加 `HUMAN_REVIEW_TASK_RECOVERED`；不会
unblock、approve、submit、claim、release、发布、升级或执行远程/破坏性动作。

相同 request id 和内容重放返回 `NO_OP`；冲突重放、第二次恢复、active/歧义/漂移都
`REFUSED` 且零写入。`PLAN_DEVIATION` 形态恢复后仍须 HUMAN 显式 `unblock`，Planner
重新提交冻结计划；后续 PLAN Reviewer 不启动复用 task。T02 完成并提交实施后，FINAL
Reviewer 才重新领取该 task，并由 FINAL review 完成它。

## 搁置、阻塞和认领

- 每个非终态都允许进入 `HELD`；搁置项不得被自动认领。
- 外部依赖未满足时使用 `BLOCKED` 并写明原因。
- 一个 WorkItem 同时最多一个活动 claim；同一仓库同时最多一个活动写锁。
- 租约过期后可由编排器回收并以更大的 generation 重新认领。

## 多 Orchestrator 协调

安装 `AWB-ORCHESTRATOR-v1` 后，多个本地 Orchestrator 可以各自通过顶层
`awb orchestrator claim <WorkItem>` 或 `claim-next` 持有不同 WorkItem。每个 WorkItem
只能有一个活动 Orchestrator lease；该 lease 与 Agent claim 分表，不授予 repository
writer 或 HUMAN gate 权限。`claim-next` 在一个 `BEGIN IMMEDIATE` 事务内按 P0→P3、
`updated_at`、WorkItem ID 选择和插入。

宿主保存返回的 generation、在到期前 `renew`，并在新 Agent claim 时成组传入
`--orchestrator-id` 和 `--orchestrator-generation`。extension 缺失或目标 WorkItem
零 lease 历史时保留旧无 fence 路径；一旦出现第一条历史，省略、缺半、owner 不同、
旧 generation、过期或已 release 全部必须在 workflow 零副作用下拒绝。已经合法取得的
Agent claim 不受 lease 后续过期、释放或接管影响。

`recover` 只接管已过期的最新 lease 并递增 generation，不释放或冒充在途 Agent。
HUMAN gate、hold/block 和同 repositoryKey 单 writer 规则保持不变。AWB 只提供一次性
本地 JSON 命令，不创建、监督、终止或迁移宿主进程和 Agent session。

## 主 Agent 活动期保障

`.awb/config.json` 的 `usagePolicy` 缺失时按 `OFF`。OFF 时不绑定 session、不创建 span、
不执行 mutation sync/show、不定期刷新、不以 coverage/credits/quota 作为 gate；历史
show/export/self-check 仍可显式调用。只有 BEST_EFFORT 延续既有脱敏采集，失败不阻断
主工作流；两种模式都不承诺 daemon 或定时 SLA。

macOS 上主 Agent 可用一个前台工具会话持有 `caffeinate -di`，并以工具 session/cell
句柄作为唯一所有权。多个活动 WorkItem 共享一个 inhibitor；只有最后一个活动项停止后
才终止并确认该精确会话。禁止 PID 文件、进程名扫描、`pkill`、猜测 PID 和 `pmset`。
启动/清理失败必须告警并停止新建 inhibitor，但不阻断 AWB workflow。非 macOS 明确降级。
这是 Agent 工具会话/Skill 行为，不是 AWB runner 或宿主进程管理 API。

## 提交契约

`submit_plan` 和 `submit_implementation` 都要求提交者持有活动 claim；二者在成功 transition 内原子释放该 claim。若存在活动 repository writer lock，必须先释放该 lock。不得先 release WorkItem claim 再提交。

每次实施提交还必须记录已通过 acceptance、测试命令/结果、实际修改范围、已知非阻断问题、关闭 Finding、复杂度变化和回归。删测、跳测、放宽断言、已有验收倒退或新同级缺陷会停止自动提交。修复需要未批准模块、契约、数据结构、基础设施或显著扩项时使用 `plan-deviation`，实现 task 进入 BLOCKED、回到 Planner，既有复审轮次不重置。

过程审计以 runtime events 为权威；不强制每轮 submission JSON、quality hash、重复
postflight 或 Release body hash。普通 WorkItem 只保留一份最终 implementation summary；
发布 WorkItem 只保留一份 release postflight。Preview 仅执行一次 clean build/full test/
fresh install、exact path allowlist、artifact member/secret scan、三资产 SHA、remote drift 和
独立 Implementation review，不要求 per-file manifest SHA、double/no-Git reproducibility 或
reachable-object/history closure。

任务只能沿已定义边推进，同一 WorkItem 最多一个 IN_PROGRESS；BLOCKED、WAITING_ACCEPTANCE、COMPLETED 或 CANCELLED 必须有证据。`show` 与 HTTP detail 投影 management、任务板、进度、currentTask、唯一 nextStep 和两阶段 review counters；board/list 只显示进度与 nextStep 摘要。

## 明确不纳入默认路线图

RunnerAuthority、逐操作 lease、进程静止证明、Git commit/tree 加密绑定、恶意 SQLite 篡改防御和分布式投影恢复不属于第一期，也不默认进入第二期；只有真实故障或明确业务要求证明必要时，才另立 WorkItem。第二期仅把同一套简单模型换成 MySQL，并增加经过身份认证的远程下指令、暂停、恢复和 steer。
