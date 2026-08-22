# Agent usage 长期观察 WorkItem 模板

> Contract: `AWB-WORKITEM-MGMT-v1`. 使用前同时阅读本目录 `README.md`。

本模板只能在 observation-only 能力完成独立 IMPLEMENTATION review 并通过 HUMAN FINAL gate 后使用。

## 启动门

- HUMAN 追加 `COHORT_STARTED`，冻结 usage/parser/rate/projection schema、UTC 起点与 cohort version。
- doctor、migration、完整测试、privacy self-check 与 dry-run 必须 PASS。
- 主 cohort 只纳入启动后首次建立 usage binding、最终 HUMAN APPROVED 的新 STANDARD WorkItem。
- READ_ONLY_DIAGNOSIS、历史项、跨期项、测试 fixture、未最终验收项均单独报告。

## 观察与退出

- 同时满足：完整 14×24 小时、至少 20 个合格 WorkItem、participation attribution coverage ≥ 90%。
- coverage 分母为 cohort WorkItem 的全部 claims 与声明必需的 ORCHESTRATOR spans；只有唯一归因且无未修复 gap 的 participation 进入分子。
- 未达任一门槛即无限延期，不自动降低标准。只有 HUMAN 可用 `COHORT_CONCLUDED(outcome=INCONCLUSIVE)` 终止。
- 每 7 天显式运行 `awb usage cohort snapshot`；无 daemon。漏跑只记录 cadence gap，不补造历史。

## 语义与结果

- 统计语义实质变化时追加 `COHORT_SEMANTICS_CHANGED`，保留旧事件并由 HUMAN 启动新 cohort version；14 天、20 项、90% 全部重算。
- 同语义数值错误使用 `USAGE_CORRECTED`，不得覆盖原事件。
- 最终报告必须分开展示 raw token、COMPLETE/PARTIAL/UNKNOWN estimated credits、quota windows、SHARED/UNATTRIBUTED、rejections、corrections、segments、P50/P90+n 与局限。
- 报告只能观察和建议；模型路由、预算、硬停、跳过 review 或削弱 HUMAN gate 必须另立获批范围。
