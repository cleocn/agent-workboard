# AWB-NNN：Agent Workboard 变更标题

> 统一执行管理规范：[`README.md`](README.md)  
> Management Contract Version：`AWB-WORKITEM-MGMT-v1`  
> Template Version：`AWB-TEMPLATE-v1`  
>
> 本文件是创建/规划输入模板。活动 WorkItem 的实时状态以 SQLite management projection 为唯一权威，不手工维护第二套运行状态。
>
> 模板契约版本：`AWB-MANAGEMENT-v1`

## 1. 身份与运行摘要

| 字段 | 值 |
| --- | --- |
| WorkItem ID | `AWB-NNN` |
| 类型 | `AWB` |
| 标题 | 待填写 |
| 模式 | `STANDARD / READ_ONLY_DIAGNOSIS` |
| 优先级 | `P0 / P1 / P2 / P3` |
| 创建时间 | `YYYY-MM-DD HH:mm:ssZ` |
| 最后更新时间 | `YYYY-MM-DD HH:mm:ssZ` |
| lifecycle state | `DRAFT` |
| queue_state | `CLAIMABLE` |
| progress | `0/N required tasks · 0%` |
| 当前任务 | `AWB-NNN-T01` |
| 唯一下一步 | `claim AWB-NNN-T01 as PLANNER` |

emoji 如需使用仅作展示，必须映射到 SQLite 中的真实 state/status。

## 2. 范围、授权与安全

### Scope

- 待填写可交付且可验证的当前范围。

### Out of scope

- 待填写明确不做事项及理由。

### Authorization

- Allowed：待填写本地读写、测试或其他已明确授权动作。
- Forbidden：默认禁止 commit、push、merge、deploy、publish、远程服务修改和远程数据写入；只有用户在当前 WorkItem 另行明确授权时才可条件执行。

### Safety constraints

- 保留用户已有修改；不得通过删测、跳测、放宽断言、吞错或移除安全检查获得通过。
- 不保存凭据、敏感原始数据或隐藏思维链。

## 3. 当前实现与证据

| 证据 ID | 对象 | 当前行为/缺口 | 核验方式 | 证据引用 |
| --- | --- | --- | --- | --- |
| E-001 | 待填写 | 待填写 | 代码、测试或运行行为 | 待填写 |

区分已确认事实、推断和未知；未知不得伪装为证据。

## 4. 编号任务板

### STANDARD

| taskId | seq | title | ownerRole | required | status | acceptance | closure evidence | next step |
| --- | ---: | --- | --- | --- | --- | --- | --- | --- |
| `AWB-NNN-T01` | 1 | 规划与诊断 | PLANNER | true | `NOT_STARTED` | 范围、方案、风险和测试可复核 | 规划产物与证据引用 | submit plan |
| `AWB-NNN-T02` | 2 | 实施与本地测试 | IMPLEMENTER | true | `NOT_STARTED` | 批准规划范围内实现且回归通过 | 修改范围、测试和质量基线 | submit implementation |
| `AWB-NNN-T03` | 3 | 批量最终复审 | REVIEWER | true | `NOT_STARTED` | 实施无有效阻断 Finding | review envelope 与关闭证据 | human final gate |

### READ_ONLY_DIAGNOSIS

| taskId | seq | title | ownerRole | required | status | acceptance | closure evidence | next step |
| --- | ---: | --- | --- | --- | --- | --- | --- | --- |
| `AWB-NNN-T01` | 1 | 规划与诊断 | PLANNER | true | `NOT_STARTED` | 诊断与建议有证据、无实施 | 规划/诊断产物 | submit plan |
| `AWB-NNN-T02` | 2 | 规划复审 | REVIEWER | true | `NOT_STARTED` | 无有效阻断 Finding | review envelope | hold at target |

创建时二选一，不把两个任务板同时持久化。任务 ID 从 T01 连续编号；同一 WorkItem 最多一个 `IN_PROGRESS`。task status 只使用 `NOT_STARTED/IN_PROGRESS/BLOCKED/WAITING_ACCEPTANCE/COMPLETED/CANCELLED`。

## 5. Acceptance 与 closure

### Acceptance criteria

| ID | 可核验标准 | 验证方式 | 当前结果 | 证据 |
| --- | --- | --- | --- | --- |
| AC-001 | 待填写 | 待填写 | 未执行 | 待填写 |

### Closure criteria

- [ ] 全部 required tasks 为 `COMPLETED`；required `CANCELLED` 不算完成。
- [ ] acceptance criteria 逐项有当前证据。
- [ ] PLAN 与 IMPLEMENTATION 的最新适用 review 已 PASS，且无开放阻断 Finding。
- [ ] scope、授权、实际修改、测试、风险和剩余问题一致。
- [ ] STANDARD 已完成人工最终 gate；READ_ONLY 已达到批准的只读目标。
- [ ] 未授权的 commit、push、merge、deploy、publish 或远程写入没有发生。

## 6. Review convergence

### Round counters

| stage | ordinary rounds used | convergence used | total rounds used | latest result |
| --- | ---: | --- | ---: | --- |
| PLAN | 0 | no | 0 | not reviewed |
| IMPLEMENTATION | 0 | no | 0 | not reviewed |

PLAN 与 IMPLEMENTATION 分别执行严格串行 `3+1+1`：第 1～3 轮普通 Reviewer；第 3 轮仍 REVISE 时冻结 artifact 并直接进行唯一一次第 4 轮 convergence review；只有 `CONVERGENCE_REVISE` 才允许一次最小修改；第 5 轮普通 Reviewer 只核验高级关闭条件及直接回归。不得重置、更换 Reviewer、新建替代 WorkItem 或重复提交绕过。

### Finding ledger

| Finding ID | stage | violated contract | evidence | impact | minimal close condition | status | origin | priorUnavailableReason |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| PLAN-F001 | PLAN | 待填写 | 待填写 | 待填写 | 待填写 | OPEN/CLOSED | `INITIAL / REVISION_REGRESSION / NEWLY_AVAILABLE_EVIDENCE` | 首轮 INITIAL 可写“不适用”；后续新 Finding 必填 |

缺任一必备字段的意见只能作为 non-blocking suggestion，不能触发 REVISE。Reviewer 不得强制偏好架构或新增需求。

## 7. 实施质量基线与复杂度变化

| 项目 | 基线/本轮结果 | 证据 |
| --- | --- | --- |
| 已通过 acceptance | 待填写 | 待填写 |
| 定向测试 | 待填写命令与结果 | 待填写 |
| 风险相称回归 | 待填写命令与结果 | 待填写 |
| 实际修改范围 | 待填写文件/模块 | 待填写 |
| 已知非阻断问题 | 待填写或“无” | 待填写 |
| 关闭 Finding | 待填写 ID | 待填写 |
| 新增文件/依赖/抽象/配置 | 待填写；逐项关联 Finding/验收 | 待填写 |
| scope 是否扩大 | no / authorized amendment | 待填写 |

返工不得让已有通过项倒退，不得削弱有效测试。新增复杂度无法追溯到需求、验收或有效 Finding 时不得引入。

## 8. PLAN_DEVIATION、BLOCKED 与人工分歧

### Escalation

- 类型：`NONE / PLAN_DEVIATION / BLOCKED / WAITING_HUMAN`
- 触发证据：待填写。
- 已完成边界：待填写。
- 未发生动作：待填写。
- 唯一下一步：待填写一个人工可执行选择。

### Disagreement table

| Finding ID | stage | Reviewer evidence | Planner/Implementer handling | current result | scope expanded | minimal choice |
| --- | --- | --- | --- | --- | --- | --- |
| 待填写 | 待填写 | 待填写 | 待填写 | 待填写 | yes/no | 待填写 |

同时报告已通过验收/测试、未关闭 Finding、已消耗轮次、继续修改风险、保持当前结果风险和退回规划影响。Agent 不代替用户裁决。

## 9. 状态历史

| 时间 | actor | lifecycle/queue | task status change | progress | 原因与证据 | 唯一下一步 |
| --- | --- | --- | --- | ---: | --- | --- |
| `YYYY-MM-DD HH:mm:ssZ` | ORCHESTRATOR | `DRAFT/CLAIMABLE` | 创建 tasks | 0% | `WORK_ITEM_CREATED` | claim T01 |

此表是创建输入/静态导出示例。活动历史由 SQLite events 追加并由 `show/timeline` 投影，禁止手工维护平行事实。

## 10. 最终结论

- 最终状态与交付：待填写。
- 已满足 acceptance/closure：待填写证据。
- 未关闭 Finding 或残余风险：待填写或“无”。
- 实际修改与测试：待填写。
- 未授权且未执行的动作：commit、push、merge、deploy、publish、远程写入；如用户后来授权，改为引用对应授权事件与实际结果。
- 唯一后续权威记录：待填写或 `NONE`。
