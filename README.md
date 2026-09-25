# 机器人作业数据回流服务

这是一个面向机器人作业数据团队的服务端应用，负责管理机型、场景、技能、作业记录、人工标注和数据集。服务使用 FastAPI 提供本地 HTTP 接口，以 SQLite 保存业务数据；质量评分、数据集审核、版本、订阅和统计分析均在同一进程内完成。

## 目录

- `main.py`：应用入口、健康检查和路由注册。
- `app/models`：业务实体及其关系。
- `app/routers`：基础资源、作业、数据集、分析和评分策略接口。
- `app/services`：评分、统计、策略目录、策略版本与时间窗口工具。
- `app/seed_data.py`：可重复执行的示例数据初始化逻辑。
- `scripts/init_sample_data.py`：初始化脚本的兼容入口。

## 评分策略版本与只读对比

模型团队调整质量评分口径时，可以先把新口径保存为策略草稿并评估影响，不必直接重算覆盖现有等级：

- `POST /api/v1/strategies` 创建策略草稿，保存权重、阈值、适用范围（机型/场景/技能，空为全局）与生效时间；同一名称下版本递增，重复版本返回 409。
- `POST /api/v1/strategies/{id}/activate` 发布草稿；范围重叠的生效版本会在同一事务内自动下线，并发激活被串行化，结果确定。
- `POST /api/v1/strategies/{id}/rollback` 回滚到已下线版本；激活、下线、回滚均写入 `/api/v1/strategy-audits` 审计记录。
- `POST /api/v1/strategy-comparisons` 在同一组作业上只读对比两个版本（草稿亦可），返回等级迁移矩阵、边界样本和数据集汇总差异，不改写任何作业的当前等级。
- 对比报告在创建时绑定输入快照（作业评分输入与摘要哈希）并落库；`GET /api/v1/strategy-comparisons/{id}` 只返回已保存内容，后续新增数据或服务重启都不会改变报告。

## 配置与运行

默认数据库文件为项目根目录的 `robot_data.db`，可以通过 `DATABASE_URL` 指定 SQLite 文件。安装依赖后运行 `python3 main.py`，服务默认监听 `8000` 端口；`GET /health` 返回服务状态，接口文档位于 `/docs`。

初始化示例数据可执行 `python3 scripts/init_sample_data.py`。该命令会重建本地数据库并写入机型、场景、技能、作业、标注及数据集示例。

## 验证

运行 `python3 -m pytest -q` 执行服务和领域工具测试，运行 `python3 -m compileall -q app main.py scripts` 检查编译。测试只使用临时 SQLite 数据库，不需要额外服务。
