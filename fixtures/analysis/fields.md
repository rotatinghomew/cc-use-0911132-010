# 业务演练数据口径

概率表示目标区间累计量大于 threshold 的概率；quantiles 键为 0 到 1 的分位水平。issued_at 是名义发行时刻，available_at 是用户真正收到的时刻。truth_modes 区分最初实况与截止时最终实况；相邻/重叠区间不能假定为独立观测。

各 ID 均为本地合成标识，不包含真实人员或生产数据。时刻使用带时区 ISO 8601；as_of 是分析可知信息的截止时刻，不是业务发生时间上界。晚到事实允许保留在输入中，但只能在 available_at 到达之后采用。source_versions 标识输入版本；baseline_case_id 关联同库既有演示运行。空值表示未知，不应未经说明转成零。
