# 住房贷款纾困申请与履约跟踪

纯Python标准库实现的住房贷款纾困申请与履约跟踪原型，使用SQLite持久化，HTTP接口由`http.server`提供。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：状态转换、偿付能力、方案阈值和履约状态和冲突检查。
- `src/ledger.py`：纾困资金台账规则，入账来源用途约束、划扣分配和缺口汇总。
- `src/repository.py`：SQLite建表、事务和查询。
- `src/service.py`：用例编排、权限检查、乐观并发和审计。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则计算、台账和失败场景测试。

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
- `GET /api/records/{id}/ledger`：纾困资金台账详情，含入账、划扣和汇总（缺口、最近扣款日）。
- `POST /api/records/{id}/ledger/entries`：登记纾困入账，请求体为`{"source":"living_allowance","amount":2000,"arrived_at":"2026-09-05","purpose":"current_arrears"}`。
- `POST /api/records/{id}/ledger/deductions`：专员发起划扣，请求体为`{"target":"current_arrears","period":"2026-09","amount":2000}`。
- `POST /api/records/{id}/ledger/deductions/{deduction_id}/confirm`：审批人确认划扣，确认后才扣减余额并更新还款计划。
- `POST /api/records/{id}/ledger/deductions/{deduction_id}/reject`：审批人驳回划扣，请求体为`{"reason":"..."}`。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。

## 纾困资金台账

贷款进入纾困执行期（`active`）后，可为记录建立资金台账，角色映射：专员=`servicer`，审批人=`underwriter`。

- 入账来源：`living_allowance`（生活补助）、`disaster_payout`（灾害赔付）、`insurance`（保险金）、`family_repayment`（家庭还款）。
- 指定用途：`current_arrears`（当期欠款）、`principal_reduction`（冲减本金）。生活补助只能补当期欠款，灾害赔付只能冲减本金；保险金和家庭还款登记时必须显式指定用途，避免资金混用。
- 划扣两段式：专员发起后为`pending`，余额和还款计划照旧；审批人确认后按"绑定来源优先、到账日先后"从指定用途的入账中分配，同一笔入账已扣部分不能再次使用；余额不足时确认失败，驳回不占用资金。
- 同一期次（`period`）的当期欠款划扣只允许一笔待确认或已确认记录，防止重复扣款、扣错月份。
- 台账详情`summary.gap`为下一期应还与当期欠款可用余额的缺口，`summary.last_deduction_at`为最近一笔已确认划扣时间；确认当期欠款划扣会冲减`arrears`，确认冲减本金会冲减`principal_outstanding`（创建记录时可选填）。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、台账入账划扣、重复引用、权限拒绝和版本冲突。
