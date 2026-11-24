# pgmemrest SQL 原生记忆治理

课程课后作业 B 方向。这个原型面向已有 `user_id` 的记忆服务，给每条结构化事实增加 `fact_key`，例如 `preferred_simulator`。服务仍负责用户认证、从对话提取事实和决定是否撤回。数据库根据已有事实执行写入决策，并在同一事务写审计。

## 运行验收

完整验收使用 PostgreSQL 16.16。课程 OpenTenBase 的分布式实例存在低权限写入兼容性限制，见[验证报告](REPORT.md)。

用有建模式和建角色权限的账号连接一个空白实验数据库。用 libpq 的本机配置提供连接信息。

```sh
psql -Xw -v ON_ERROR_STOP=1 -d "$DEMO_DB" -f install.sql -f test.sql
```

测试创建三个临时验收角色、写入虚构事实，再回滚数据和角色。实验库保留空的治理模式，以便继续演示。生产接入时先由管理员创建同名 `NOLOGIN` 权限组，再由模式所有者运行 `psql -Xw -1 -v ON_ERROR_STOP=1 -f grants.sql`，最后将权限组授予对应的服务账号。角色名称在数据库集群范围内共用，验收使用空白测试环境。

## 写入与查询

应用服务使用 `pgmemrest_writer` 权限，通过 `inbox` 插入并读取本次决策。写入触发器在保存回执前清除输入字段，回执只保留动作和时间。服务账号只获准读取回执中的动作。

```sql
INSERT INTO memory_governance.inbox (user_id, fact_key, category, memory_text, intent)
VALUES ('demo-user', 'preferred_simulator', 'work', 'Uses simulator A', 'assert')
RETURNING decision;

SELECT fact_key, memory_text, retention_score
FROM memory_governance.active_memories
WHERE user_id = 'demo-user'
ORDER BY retention_score DESC;
```

| 输入 | 触发器动作 |
| --- | --- |
| 新事实或重新确认已归档事实 | `ADD`，创建或恢复当前条目 |
| 同一用户、同一事实键，类别或内容已变更 | `UPDATE` |
| 同一事实的内容和类别保持一致 | `NOOP`，保留上次实际变更时间 |
| 显式 `retract` 现有活动事实 | `DELETE`，以归档方式撤回 |
| 撤回不存在或已归档的事实 | `NOOP` |

`fact_key` 和类别去首尾空格并转小写，内容合并连续空白。结构化事实键由接入方提供，文本归并仅按规范化后的精确内容做匹配。默认目标是同一写入协调节点上的常规 `READ COMMITTED` 事务。服务按请求参数绑定 SQL，并用已认证身份约束 `user_id`。写入角色是面向后端服务的权限组，拥有活动视图的读取权限。

## 时间衰减与压缩

活动视图的保留分按距最近一次有效写入的时间计算。30 天半衰期对应 60 天时的 0.25 阈值。归档过程限制每批处理行数，跳过正被其他事务锁住的行。为实现定时归档，可在维护账号的 libpq 服务配置 `pgmemrest-governance` 下安装以下 cron 任务，每 15 分钟处理至多 500 条。退出码非零表示该轮执行失败。

```cron
*/15 * * * * PGSERVICE=pgmemrest-governance /usr/bin/psql -Xw -v ON_ERROR_STOP=1 -c 'SELECT memory_governance.archive_stale(500);'
```

`compress_user('demo-user')` 由维护账号调用。对同一个用户、同一种类别下内容完全相同的活动条目，`row_number()` 保留最近更新的一条。更新时间并列时按事实键排序。其他行被标记为归档，并从活动视图中移出。归档保留当前行和原因，不进行物理删除。重新录入同一事实会复用该行。

## 审计权限

基表的 `AFTER` 触发器记录各类更改，写入触发器记录接受但未改数据的 `NOOP`。只读审计员通过 `audit_log` 查看认证数据库账号、事实键、动作和时间。日志和数据同事务提交或回滚，日志无需保存记忆正文。

写入账号不能直接修改基表、停用触发器或清空审计日志。维护账号只能从已授予的函数执行归档和去重。权限边界适用于这些应用角色。数据库所有者和管理员拥有更高权限，生产环境应把这些身份与服务账号分离。单协调节点以事务级 advisory lock 串行处理同一用户的写入和压缩。多写入协调节点的并发行为需要额外验证。

参考 [pgmemrest 记忆服务](https://github.com/OpenTenBase/pgmemrest/tree/main/openclaw_plugin/docker) · [PostgreSQL 触发器](https://www.postgresql.org/docs/16/sql-createtrigger.html) · [PostgreSQL 窗口函数](https://www.postgresql.org/docs/16/functions-window.html)
