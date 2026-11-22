# 养老服务湖的撤回清除与存续数据资料包

养老服务数据湖的受控资料、派生交付副本与撤回清除处理程序。

data/ 是合成来源资料，docs/source-contract.md 解释来源身份、适用范围和业务含义。lakeops/ 是业务程序：从来源资料初始化湖，执行撤回清除、接收后续资料、构建受管交付、查询处理状态与文件扫描核对。docs/operations.md 是操作说明，含状态口径（仅限制访问 vs 已完成清除）与恢复语义。support/check_inputs.py 只检查来源资料可读性和引用，不生成仓表或业务答案。tests/ 覆盖资料自检与业务演练场景。

## 环境与使用

Python 3.14 标准库，无需在线安装依赖。Dockerfile 锁定基础镜像完整摘要；首次构建需要本地镜像或拉取该摘要。

    python support/check_inputs.py
    python -m unittest discover -s tests -v
    docker build -t care-lake-erasure-inputs .
    docker run --rm --network none care-lake-erasure-inputs

容器默认进行资料自检；可在镜像名后加 python -m unittest discover -s tests -v 运行全部测试。

## 业务入口速览

    python -m lakeops init --root runtime --source data --now 2026-11-05T09:00:00+08:00
    python -m lakeops purge --root runtime --request E1 --now 2026-11-07T09:00:00+08:00
    python -m lakeops drill --root runtime --copy C3 --availability available   # 演练可达性
    python -m lakeops purge --root runtime --request E1 --now 2026-11-08T09:00:00+08:00
    python -m lakeops ingest --root runtime --delivery D2 --now 2026-11-11T10:00:00+08:00
    python -m lakeops status --root runtime
    python -m lakeops verify --root runtime --request E2 --now 2026-11-08T12:00:00+08:00

运行目录（默认建议 runtime/，已加入 .gitignore）包含湖文件 lake/ 与受限运行状态 state/。清除、接收后续资料、交付、查询与核对的完整说明见 docs/operations.md。状态查询区分“仅限制访问”（已限制新交付，物理清除未完成）与“已完成清除”（范围内副本均已改写）；保留例外、到期续清与核对记录的口径同样在 operations.md。
