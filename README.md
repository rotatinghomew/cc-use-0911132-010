# 逐次订正预报的概率技巧与时效价值

项目使用 Python 3.11 标准库与 SQLite，现有逐案处理入口为 `progressive_forecast_chain.release.cli`。保留原有处理器、持久化与测试；分析业务使用本地合成资料，不连接生产系统。无需 pip 安装、下载模型或启动额外服务。

## 现有程序

在仓库根目录运行 `bash scripts/demo.sh`，依次完成现有产品回归、分析输入核对、语法构建和真实业务入口调用。所有运行文件位于临时目录，结束后清理；运行失败立即退出，不使用预存输出代替执行。

单独测试：`python3 -m unittest discover -s tests/forecast_vintages -p 'test_*.py' -v`。

输入检查：`python3 -m unittest discover -s tests -p test_analysis_inputs.py -v`。

构建检查：`python3 -m compileall -q progressive_forecast_chain tests`。

业务调用：`python3 -m progressive_forecast_chain.release.cli fixtures/demo.json --database /tmp/progressive_forecast_chain-demo.sqlite3`。

## 分析资料与联调

`fixtures/analysis/input.json` 保存业务演练数据，字段解释在 `fixtures/analysis/fields.md`。以下已核对的数据形态用于分析回归：late_products、truth_revision、partial_target。现有输入测试检查这些形态是否真实存在，不代替分析方法或数值正确性的验证。

分析入口接通后执行 `bash scripts/feature_demo.sh`：先重跑现有产品，再从干净数据库计算分析、同输入重放、核对输出结构并打印完整 JSON。尚未接通时该命令会报模块不存在，不会用静态样例冒充成功。分析 ID 为 `analysis_id`，其余输出包括 `matched_products, scores, calibration, revision_gains, sample_support`。方法正确性还需覆盖业务说明中的反例，结构核验不能代替统计验证。

## 离线环境

使用 Python 3.11.2 的 Linux 环境即可复现。容器环境预置解释器后，以只读方式挂载仓库，将文件复制到容器临时目录再运行脚本；运行时关闭网络。基础测试和小规模本地数据适合在 90 秒内展示，正式展示命令为 `bash scripts/feature_demo.sh`。不要在展示脚本里安装依赖或等待外部服务。
