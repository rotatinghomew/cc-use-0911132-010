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

## 概率预报价值分析（forecast_value）

入口：

```
python3 -m progressive_forecast_chain.forecast_value \
  --input fixtures/analysis/input.json --database <product_db> --output <value_json>
```

分析只使用标准库，结果同时写入 `<product_db>` 的 `forecast_value_analyses` 与 `forecast_value_matches` 两张新表，并通过 `baseline_case_id` 连接既有 `runs` 表中的 vintage / lineage 运行记录；同输入重放返回同一 `analysis_id` 且输出字节一致。

### 配对与口径

* 配对按用户真正收到产品的 `available_at` 与研究指定的决策提前量（24h/72h）判定，迟到产品在对应提前量上以 `not_available_at_decision_lead` 显式排除，绝不被事后最优版本替换；每个目标只取决策时刻可得的最新 vintage。
* 仅当产品 `target_start/target_end` 与实况区间完全一致时才配对；累积时段不同（如 `partial-target` 的 12 小时窗口）以 `no_common_coverage_window` 整体排除，不与任何全日实况值比较。
* 实况保留两种口径：`first_available`（最初实况）与 `final`（截止 `as_of` 的最终实况，含追认修订）。被修订目标在两种口径下可以得到相反结论，输出逐目标保留 `truth_id` 以便解释。
* 历史基线是仅用 `training_end` 之前目标估计的气候概率（事件率与气候分位数），评估期冻结，不允许评估期信息回流。

### 评分、校准与增益

* 准确性：Brier 分数（主指标，含按 `event_id` 聚类 bootstrap 的 90% 区间）与相对气候基线的 Brier 技巧评分 BSS；ROC AUC 作区分度辅助，单类事件时返回 null。
* 校准：固定五档可靠性分组（样本不足的分组标记 `support_sufficient=false`）、Murphy 可靠性-解析度-不确定性分解，以及 0.1/0.5/0.9 分位数覆盖率与 pinball 损失。
* 临近更新增益：沿 `revision_of` 版本链计算共同样本上的 Brier 降幅，并标注订正版本在名义提前量上是否准时；增益以订正版本的真实有效提前量为条件，短提前量天然更容易预测的优势不计为产品改善。
* 共同样本与不确定性：模型排名只在共同样本上做配对 Brier 差，按 `event_id` 聚类重采样（同一过程的多时效、相邻区间不视为独立观测）；共同样本少于 8、聚类少于 4 或置信区间包含 0 时只报 `withhold_*` 不排名（72h 提前量上 late 版本完全不可得，即不排名）。

### 演练中的两个反例

* `product-*-1-1`（late，名义 24h）在最初实况口径下几乎百发百中，但实际提前量只有 6 小时，24h 决策点尚未收到，不能赢得 24h 比较；其增益只出现在真实可得的场景，并在 `final` 口径下对被修订目标（truth-20：4→17）转为负增益。
* `partial-target` 只覆盖 12 小时窗口，与所有 24 小时实况区间都不重合，被整体排除，无法参与任何评分或排名。

方法测试：`python3 -m unittest discover -s tests/forecast_value -p 'test_*.py' -v`。

## 离线环境

使用 Python 3.11.2 的 Linux 环境即可复现。容器环境预置解释器后，以只读方式挂载仓库，将文件复制到容器临时目录再运行脚本；运行时关闭网络。基础测试和小规模本地数据适合在 90 秒内展示，正式展示命令为 `bash scripts/feature_demo.sh`。不要在展示脚本里安装依赖或等待外部服务。
