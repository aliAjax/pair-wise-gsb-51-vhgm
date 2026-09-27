# 住房贷款纾困申请与履约跟踪

纯Python标准库实现的住房贷款纾困申请与履约跟踪原型，使用SQLite持久化，HTTP接口由`http.server`提供。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：状态转换、偿付能力、方案阈值和履约状态和冲突检查。
- `src/repository.py`：SQLite建表、事务和查询。
- `src/service.py`：用例编排、权限检查、乐观并发和审计。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则计算和失败场景测试。

## 启动

```bash
python3 app.py --db ./data.db --port 8327
```

默认端口为`8327`，默认数据库位于项目目录。服务启动时自动建表。

## 主要接口

- `GET /health`：健康检查。
- `GET /`：演示页面。
- `GET /api/records`：记录列表，可带`state`和`limit`参数。
- `GET /api/records/{id}`：记录详情。
- `GET /api/records/{id}/audit`：审计时间线。
- `GET /api/stats`：状态统计。
- `POST /api/records`：创建记录，请求体为`{"reference":"...","data":{...}}`。
- `POST /api/records/{id}/actions/{action}`：执行业务动作，请求体为`{"expected_version":1,"data":{...}}`。

## 纾困资金台账

记录进入`active`（纾困方案生效）后开放资金台账，按笔管理入账与划扣：

- `GET /api/records/{id}/ledger`：台账详情，含入账列表、划扣列表和汇总（下一期应还、可用余额、缺口、最近扣款日、待审批笔数）。
- `POST /api/records/{id}/ledger/entries`：专员（`servicer`）登记入账，请求体为`{"source":"living_allowance","purpose":"current_arrears","amount":5000,"received_at":"2026-09-01","note":"..."}`。
- `POST /api/records/{id}/ledger/deductions`：专员发起划扣，请求体为`{"entry_id":1,"amount":3000,"target_period":"2026-09"}`，生成待审批划扣单，余额与还款计划不变。
- `POST /api/records/{id}/ledger/deductions/{did}/confirm`：审批人（`underwriter`）确认划扣，请求体为`{"expected_version":4}`，确认后才扣减入账可用金额并更新欠款/本金。
- `POST /api/records/{id}/ledger/deductions/{did}/reject`：审批人驳回，请求体可带`{"reason":"..."}`，驳回后该期次可重新发起。

资金规则：

- 来源与用途绑定：`living_allowance`（生活补助）只能补当期欠款，`disaster_payout`（灾害赔付）只能冲减本金，`insurance`（保险金）两者皆可，`household_payment`（家庭还款）只能补当期欠款。
- 同一笔入账的可用金额只在审批确认时扣减一次；同一入账同一期次只允许一笔有效划扣，已扣过的期次不能重复划扣。
- 发起人不能审批本人发起的划扣单；确认时重新校验可用余额与记录版本。
- 缺口 = 下一期应还（批准后为`approved_payment`）减去可用于当期欠款的余额，灾害赔付等冲本金资金不计入。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、重复引用、权限拒绝和版本冲突。
