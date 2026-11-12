# 登记交换与资产授权台账

台账区分接收序列和入账序列。机构事件按修订应用，授权具有生效及到期时间；知识时点查询用于重建当时可知的登记。

运行需要 Python 3.11 或更高版本，使用标准库与 SQLite，无需第三方包。演练中的机构、人员、交易及读数全部为本地合成数据。

## 构建与测试

```sh
python3 -m compileall -q registry_bridge
python3 -m unittest discover -s tests -v
```

## 本地运行

```sh
python3 -m registry_bridge --db :memory: demo --fixture fixtures/demo.json
python3 -m registry_bridge --help
```

演练使用临时数据库，不会连接远程服务。命令以 JSON 显示业务结果；测试覆盖核心状态、边界输入和错误路径。运行前可用 `python3 --version` 核对解释器版本。

## 分批上线效果研究

研究对象是每个机构每周“登记资产的有效可用天数 / 观察资产天数”，全部由已入账事件重建，不改变登记状态。业务周与知识截止分开固定：事件按业务 `effective_at` 落入周格，只纳入 `applied_at` 不晚于知识截止的事件；上线日期来自运营单独登记的追加表，不根据结果反推。

- 观测从资产注册当天开始，撤销当天结束；可用期等于状态为 active 的区间与授权 `expires_at` 的交集。
- 暂停、撤销、周中注册或更正只计入整日，部分日不进任一计数，因此不会被当成整周记录。
- 处理队列按首次启用周形成；对照只取从未启用或在该相对周（及基期周）尚未启用的机构，已启用机构不会回头给晚启用队列当对照。

```sh
python3 -m registry_bridge --db :memory: call rollout.record --json '{"issuer":"north","launched_at":"2026-06-15T00:00:00Z","source":"rollout-plan"}'
python3 -m registry_bridge --db :memory: call study.create --json '{"name":"q3","start":"2026-06-01T00:00:00Z","end":"2026-10-19T00:00:00Z","knowledge_cutoff":"2026-11-12T00:00:00Z"}'
python3 -m registry_bridge --db :memory: call study.get --json '{"name":"q3"}'
python3 -m registry_bridge --db :memory: call study.cohort --json '{"name":"q3","cohort_week":8}'
python3 -m registry_bridge --db :memory: call study.week --json '{"name":"q3","relative_week":2}'
python3 -m registry_bridge --db :memory: call study.issuer --json '{"name":"q3","issuer":"north"}'
```

研究创建时固化事件集合、上线表、选项、基期、对照规则、聚合权重、置信水平与随机种子，并保存定义与结果摘要。补报或更正后新建研究即可；旧研究用 `study.replay` 从原冻结输入复算，用 `study.verify` 校验存储摘要。`study-export` 复用现有导出写出报告，包含各相对周的分子分母、点估计、逐点区间、同时区间及构造说明，并把双重差分与朴素前后差并排展示。

结论依赖平行趋势和无提前反应假设；处理前差异不显著不代表假设成立。没有共同支持的队列×相对周保留为缺口，不做外推或补满。自助法按机构整体重采样，同一机构的周序列不拆散；机构太少或有效重采样不足时，推断被限制并给出原因。

## 演练：提前改善不等于推广收益

```sh
python3 -m registry_bridge --db :memory: study-drill --name rollout
python3 -m registry_bridge --db :memory: study-export rollout report.md
```

演练数据包含两类机构：一类在上线前已完成改善、上线后没有额外效果；另一类在上线后才改善。前者在朴素前后差中会显示为收益，但在处理前事件研究中出现违约，且上线后队列内双重差分为零；后者只在上线后出现正效应。另有从未启用机构负责对照，并包含撤销、授权到期和周中注册，用于核对分子分母口径。
