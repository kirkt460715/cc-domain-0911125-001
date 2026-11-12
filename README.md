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
