# C 方向：Apache AGE 在 OpenTenBase 上的图级分片适配

提交人：付格丞（2601210631），GitHub：[@gechengfu](https://github.com/gechengfu)。

本作业把 **Apache AGE 本身**适配到 OpenTenBase 5.21.8，并实现 CN 路由、DN 执行原生 Cypher 的图级分片方案。适合将一个用户的记忆组织成一张图，不同用户的图分布在不同 DN。保留 AGE 的解析器、执行器、`agtype`、顶点、边和可变长度路径查询。

**范围：整张图位于一个 DN，单张图内部不再分片。** 支持同一事务访问不同 DN 上的多张图；不支持一条 Cypher 跨 DN 遍历同一张图。本实现是课程实验原型，不能视为 Apache AGE 全功能的分布式移植或生产可用版本。完整取舍见 [设计与实测报告](REPORT.md)。

已在免费本地 OpenTenBase **1 GTM + 1 CN + 2 DN** 实际运行：20 项自动测试通过。没有用 SQLite、普通 PostgreSQL或模拟数据库代替分布式验收。实现、测试和文档使用 Codex 辅助完成。

## 交付内容

| 文件 | 用途 |
| --- | --- |
| `patches/apache-age-pg11-opentenbase.patch` | Apache AGE PG11 的接口兼容、图本地序列及分布式路由补丁 |
| `scripts/prepare.py`、`upstream-files.json` | 获取/复制并验证 123 个固定版本上游构建文件，应用补丁 |
| `scripts/build.sh` | 使用 OpenTenBase 的 PGXS 清理并编译 |
| `sql/install.sql` | 路由注册表、CN/DN 函数及权限设置 |
| `sql/demo.sql` | 双节点记忆图、多跳查询及回滚演示 |
| `tests/test_cluster.py` | 分片、Cypher、事务、并发、输入及权限的真实集群测试 |
| `evidence/` | 实际构建日志、20 项测试输出、逐条 SQL 记录、演示输出 |

## 构建与部署

需要现成的实验 OpenTenBase 集群及管理员账号。编译机须有与各节点一致的架构、OpenTenBase 服务端头文件/PGXS、GCC、make、flex、bison、Perl、Git；准备脚本需要 Python 3。建议使用独立实验数据库，正常业务数据库无需修改。

以下命令在本目录执行。`PG_CONFIG` 必须指向 **OpenTenBase** 的二进制，而非系统 PostgreSQL。

```bash
# 方法一：从固定提交获取公开构建文件，逐个验证 Git blob 哈希。
python3 scripts/prepare.py --download --output /tmp/age-opentenbase-build

# 方法二：离线/已有上游 checkout 时使用（和上面二选一）。
# python3 scripts/prepare.py --source /path/to/apache-age --output /tmp/age-opentenbase-build

export PG_CONFIG=/path/to/opentenbase/bin/pg_config
bash scripts/build.sh /tmp/age-opentenbase-build
make -C /tmp/age-opentenbase-build PG_CONFIG="$PG_CONFIG" install
```

固定 Apache AGE 源码为 [`1bb3aff56470499a1c036ae36eee4dfeeb9566b4`](https://github.com/apache/age/tree/1bb3aff56470499a1c036ae36eee4dfeeb9566b4)，PG11 分支，扩展版本 `1.5.0`。准备脚本拒绝不匹配的源文件，也不会覆盖已有输出目录。它仅准备构建输入，不复制上游驱动、图片或回归数据；Apache 原始 `LICENSE`、`NOTICE` 保留。

**在所有 CN/DN 主机上安装同一份构建结果**，包括备用节点所在主机。可以分别编译；相同 ABI/架构下也可复制 `age.so` 到 `pg_config --pkglibdir`，以及 `age.control`、`age--1.5.0.sql` 到 `$(pg_config --sharedir)/extension/`。只在 CN 安装文件是不够的。升级已加载的实验库前关闭实验会话、清理该实验数据库的池连接；不要覆盖正在被业务会话使用的共享库。

通过 CN 新建并连接独立数据库，再安装 SQL 接口：

```bash
export PGHOST=127.0.0.1 PGPORT=11003 PGUSER=opentenbase
createdb age_course_lab
export PGDATABASE=age_course_lab
psql -X -v ON_ERROR_STOP=1 -f sql/install.sql
```

使用 `PGPASSWORD`/`.pgpass` 等常规连接配置时不要把密码写进脚本或提交记录。`sql/install.sql` 面向首次安装；已有 `age_dist` 时会报错，不会删除既有图。

## 调用示例

```sql
-- 默认按图名哈希在当前 DN 列表中选择节点，并持久保存路由。
SELECT age_dist.create_graph('alice_memory');
-- 也可以显式指定实际存在的 DN：
-- SELECT age_dist.create_graph('bob_memory', 'dn0002');

SELECT age_dist.cypher('alice_memory', $$
  CREATE (a:Memory {key:"preference",value:"tea"})
    -[:SUPPORTS]->(b:Memory {key:"habit",value:"morning"})
    -[:SUPPORTS]->(c:Memory {key:"goal",value:"health"})
$$);

SELECT age_dist.cypher('alice_memory', $$
  MATCH (:Memory {key:"preference"})-[:SUPPORTS*1..2]->(m)
  RETURN m.key, m.value ORDER BY m.key
$$, 2);
-- 第三个参数是 RETURN 列数（1～32），默认 1。

SELECT graph_name, data_node FROM age_dist.graph_routes;
-- 仅在确认不再需要该图时执行：
-- SELECT age_dist.drop_graph('alice_memory');
```

返回类型是 JSONB 行数组，列名为 `c0`、`c1`……，每个非空值保存 **AGE 原始文本表示**。例如字符串 `tea` 对应 `"\"tea\""`；顶点/路径保留 `::vertex` / `::path` 类型标记，不能把这些内容直接当普通 JSON。SQL NULL 对应 JSON null；无返回行对应 `[]`。不含 RETURN 的写语句使用默认列数 1。

使用 `age_dist` 路由接口，不要在 CN 直接调用 `ag_catalog.cypher` / `ag_catalog.create_graph`；补丁会明确拒绝这种用法。图的物理 schema/标签表和 AGE 本地目录位于所属 DN，CN 不提供这些物理表的普通 SQL 访问。

## 复验

```bash
# 只创建临时演示图，并在结束时整体回滚。
psql -X -f sql/demo.sql

# 至少两个 DN；需要实验管理员；测试会清理自己创建的唯一前缀图。
AGE_EVIDENCE=/tmp/age-sql-records.json python3 tests/test_cluster.py
```

如需要通过容器/SSH 调用 `psql`，可以设置 `PSQL=/path/to/executable-wrapper`；wrapper 必须转发标准输入、参数及退出码。测试本身没有 mock，也不下载额外 Python 包。

实测结果见 [tests.txt](evidence/tests.txt)、[逐条 SQL 记录](evidence/sql-records.json)、[demo.txt](evidence/demo.txt)。测试覆盖原生 Cypher CREATE/MATCH/SET/MERGE/DELETE、变长路径、双节点事务提交/回滚、出错回滚、重复建图、并发 ID（3 轮、共 36 次写入）、重复建图竞争（3 轮）、元数据副本、特殊字符和只读/低权限拒绝。

## 当前边界

- **图级分片**，不支持跨分片单图遍历、跨图 Cypher JOIN、自动迁移/再均衡。新增节点只影响以后创建的图。
- 每张图的请求在所属 DN 用事务级锁串行执行，避免原生 AGE 隐式创建标签的并发冲突；热点大图存在单节点与锁竞争瓶颈。多图事务应使用一致的访问顺序以减少死锁。
- 仅限超级用户实验环境，未实现租户授权；“按用户建图”只是组织与放置方式，不是安全隔离。勿直接作为公开服务接口。
- 所有图请求使用读写事务路径；只读事务会被拒绝。结果一次性聚合返回，应使用 `LIMIT` 控制规模。
- 本次验证为单 CN、两个主 DN；没有验证多 CN、故障切换、扩缩容、备份恢复或 Apache AGE 全套回归。CSV 导入、Cypher 参数绑定以及其他 AGE 辅助接口不属于此次路由 API 的支持范围。
- CN 的普通 `pg_dump` 不能代表 DN 本地图的完整备份；备份恢复方案仍需专门适配。不要把这个原型部署到存放重要数据的环境。

## 来源与许可

Apache AGE 原始代码及其补丁中的修改按 Apache-2.0 使用，见 [LICENSE](patches/Apache-2.0.txt) 与 [NOTICE](patches/NOTICE)。本目录的 SQL、Python、Shell 辅助文件按本仓库 MIT 许可发布。参考 [Apache AGE 安装文档](https://age.apache.org/age-manual/master/intro/setup.html) 与 [OpenTenBase 源码](https://github.com/OpenTenBase/OpenTenBase)。

社区已有 [OpenTenBase #556](https://github.com/OpenTenBase/OpenTenBase/pull/556) 的 AGE 风格图接口方案。本作业未复制该 PR 的实现，而是从上述固定 Apache AGE 源码应用兼容补丁，采用整图分片策略；二者目标范围不同。
