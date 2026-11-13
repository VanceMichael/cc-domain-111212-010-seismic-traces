# seismic-traces

地震剖面文件的变长道数据接收所用的来源检查与原件留存工具。

## 当前能力

`source.py` 查看来源的现有结构，不把检查结果冒充完整业务采集。
`traces.py` 按来源剖面约定解释道头与样本：区分卷头默认值与道头覆盖值，处理 IBM/IEEE/整数样本格式，解释坐标比例因子、坐标单位与采样间隔，并维护绑定文件指纹与解释配置的可恢复道索引；未知格式、越界道长与不确定端序一律报告，不跳读重同步。
`storage.py` 将原始字节、来源标识、摘要和检查结果一起保存在本地 SQLite，重复原件保留同一收件编号。
`sampledata.py` 构造可重复的本地数据；`transport.py` 提供独立的回环 HTTP 演练源与请求记录。
解释范围和来源变体见 `docs/source-contract.md`，当前检查测试与将来新增业务能力的测试应分开保留。

## 环境

Python 3.11 或以上，仅标准库，无需凭据和外部数据。使用容器时可执行 `docker build -t seismic-traces .`，依赖及镜像须在正式运行前准备。
运行入口、测试与演示均不安装包，不访问公网。数据库使用临时路径，不向代码目录写入业务文件。

## 测试与构建

构建工具版本固定在 `requirements-build.txt`，在环境准备阶段执行 `python3 -m pip install -r requirements-build.txt`；容器构建已包含此步骤。应用运行仍仅使用标准库，测试及演示不安装依赖。

```sh
python3 -m unittest discover -s tests -v
python3 -m compileall -q app.py source.py sampledata.py storage.py transport.py scenario.py traces.py
python3 -m build --wheel --no-isolation
```

## 快速演示

```sh
python3 app.py demo
python3 app.py trace-demo
```

`demo` 真实启动本地来源、领取原件、执行检查并保存临时数据库，展示检查结果与重复收件身份，退出后自动清理。正常在数秒内完成，不以预录日志代替执行。
`trace-demo` 演示道索引与抽取：变长道、负比例因子、标量零不清零、截断文件的可读道与未验证交付区分，以及抽取测线道段后与原件逐字节一致的回读校验。

## 单独检查与收件

```sh
python3 app.py fixtures /tmp/seismic-traces-fixtures
python3 app.py inspect /tmp/seismic-traces-fixtures/sample.sgy
python3 app.py capture /tmp/seismic-traces-fixtures/sample.sgy --database /tmp/seismic-traces.sqlite3 --source local-partner
python3 app.py list --database /tmp/seismic-traces.sqlite3
```

## 道索引、预览与抽取

```sh
python3 app.py index /tmp/seismic-traces-fixtures/lines.sgy --database /tmp/trace-index.sqlite3
python3 app.py preview /tmp/seismic-traces-fixtures/lines.sgy --database /tmp/trace-index.sqlite3 --line 7 --samples 2
python3 app.py extract /tmp/seismic-traces-fixtures/lines.sgy --database /tmp/trace-index.sqlite3 --line 7 --first 2 --last 3 --output /tmp/line7.segy
```

`index` 建立或恢复绑定文件指纹与解释配置的道索引；`preview` 列出已索引道并区分已验证完整交付与仅可读道；`extract` 抽取指定测线道段的原始字节写入输出文件（不覆盖已有文件）。解释配置可用 `--config '{"byte_order":"little","extended_headers":2,"max_samples":100000}'` 覆盖；索引语义见 `docs/source-contract.md`。

生成夹具不会覆盖已有文件。运行错误以 JSON 输出到标准错误并返回退出码 2；原始输入无效时不生成成功收件。内部原件与任何对外展示副本应分开管理，不写入真实个人数据。
