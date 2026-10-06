# 统筹区域创新联合投入协作基础服务

本项目提供科技创新协作场景共用的服务端基础能力，用于登记科研机构、创新节点、操作者和结构化业务资料，并提供角色权限、请求幂等、SQLite 事务与哈希串联审计。重大项目、科研证据、成果转化和国际合作等领域可以在这些稳定边界上扩展自己的状态、规则和接口。

在此之上，`joint.py` 实现区域创新联合投入与履约服务：登记合作章程版本、成员资格与投入上限、人才工时、设备时段、资金来源（含用途限制与成果落地条件）、里程碑需求、承诺与会签、履约记录、扰动事件和成果权益分配。核心规则：

- 同一人才、设备、资金按全局唯一标识只登记一次，所有承诺从统一额度池扣减，各地不能把同一位专家、同一台设备重复计入配套承诺；
- 承诺经全体成员会签后才占用可分配额度，草稿不占额度；
- 成员上限按聚合口径校验，拆分承诺不能绕过；
- 同一资源跨计划冲突时按已冻结的优先规则裁定，规则冻结后不可更改；
- 人员离任、资金延期、设备停机、部分履约和成员退出只重算未来义务，已完成的投入与成果分配不得回写；
- 可按历史日期复原当时有效的章程版本与承诺。

## 目录

- `src/science_strategy_foundation/`：领域模型、SQLite 存储、权限服务、审计链、联合投入履约服务、HTTP 路由和离线验收；
- `tests/`：基础规则、事务边界、接口路由、联合投入规则和端到端验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅使用 Python 标准库和 SQLite

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 构建检查

```bash
python3 -m compileall -q src tests
```

## 离线验收

```bash
PYTHONPATH=src python3 -m science_strategy_foundation.acceptance
```

验收命令会在临时 SQLite 数据库中登记科研机构、操作者、创新节点和业务资料，并执行三区域共建平台的联合投入链：登记章程与成员、登记专家/设备/企业资金、拦截重复登记、会签生效承诺、拦截超额承诺、登记履约与设备停机、分配成果权益、办理成员退出，最后核对里程碑保障、退出影响与历史复原，成功时输出一行 `status` 为 `ok` 的 JSON 并以退出码 `0` 结束。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m science_strategy_foundation.api --database science_strategy.sqlite3 --host 127.0.0.1 --port 8080
```

健康检查使用 `GET /health`。写入接口通过 `X-Actor-Id` 标识操作者，服务重启后 SQLite 中的业务状态和审计历史继续保留。

联合投入接口（写入均为 POST，查询均为 GET）：

- `/joint/charter-versions`、`/joint/members`、`/joint/resources`、`/joint/milestones`、`/joint/priority-rules`：登记章程版本、成员、资源、里程碑和冻结优先规则；
- `/joint/commitments`、`/joint/countersignatures`、`/joint/commitment-closures`：承诺草稿、会签与关闭；
- `/joint/fulfillments`、`/joint/disruptions`、`/joint/distributions`：履约登记、扰动事件与成果权益分配；
- `/joint/milestone-readiness`、`/joint/member-position`、`/joint/exit-impact`：里程碑资源保障、成员投入头寸与退出影响；
- `/joint/charter-at`、`/joint/commitments-at`、`/joint/resource-capacity`：按历史日期复原章程与承诺、查看资源额度。
