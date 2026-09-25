# 机器人作业数据回流服务

这是一个面向机器人作业数据团队的服务端应用，负责管理机型、场景、技能、作业记录、人工标注和数据集。服务使用 FastAPI 提供本地 HTTP 接口，以 SQLite 保存业务数据；质量评分、数据集审核、版本、订阅和统计分析均在同一进程内完成。

## 目录

- `main.py`：应用入口、健康检查和路由注册。
- `app/models`：业务实体及其关系。
- `app/routers`：基础资源、作业、数据集、分析和质量策略接口。
- `app/services`：评分、统计、策略目录、质量策略版本管理与时间窗口工具。
- `app/seed_data.py`：可重复执行的示例数据初始化逻辑。
- `scripts/init_sample_data.py`：初始化脚本的兼容入口。

## 配置与运行

默认数据库文件为项目根目录的 `robot_data.db`，可以通过 `DATABASE_URL` 指定 SQLite 文件。安装依赖后运行 `python3 main.py`，服务默认监听 `8000` 端口；`GET /health` 返回服务状态，接口文档位于 `/docs`。

初始化示例数据可执行 `python3 scripts/init_sample_data.py`。该命令会重建本地数据库并写入机型、场景、技能、作业、标注及数据集示例。

## 质量策略版本与只读对比

模型团队调整评分口径时，可以在不触碰线上等级的前提下先评估影响：

- **策略版本**：`POST /api/v1/quality-policies` 创建草稿，保存权重、A/B/C 阈值、适用范围（机型/场景/技能的任意组合，缺省为全局）与生效时间；草稿可用 `PUT` 修改（携带 `expected_content_hash` 做乐观并发控制），激活后内容不可变。
- **只读对比**：`POST /api/v1/quality-comparisons` 选择两个版本（草稿也可参与），在同一组作业（显式 ID、数据集或过滤条件）上计算：
  - 等级迁移矩阵（A/B/C/D → A/B/C/D）与逐条 `transition`；
  - 边界样本（质量分距任一版本阈值不超过 `boundary_tolerance`）；
  - 每个相关数据集的等级分布、主导等级和平均分差异。

  比较全程只读，不会改写 `operation_data.data_grade`。结果连同**输入快照**（参与打分的作业字段、标注字段、两版本内容哈希、选择条件）和 `snapshot_hash` 一并固化；`GET /api/v1/quality-comparisons/{id}` 只反序列化已保存内容，不重算、不重查，因此后续新增数据或修改标注都不会改变已保存的报告。
- **发布与并发**：`POST .../{id}/activate` 处理范围重叠——范围互不相交的策略允许共存；新范围完全覆盖旧范围时需显式 `force_replace`，部分重叠或更宽范围一律拒绝（409）。进程锁串行化激活，数据库部分唯一索引 `uq_quality_policy_active_scope` 兜底同范围并发激活；版本号分配亦有锁与唯一约束保护。
- **回滚与审计**：`POST .../{id}/rollback` 可重新激活历史版本；创建、改稿、激活、取代、回滚以及**激活失败**都会写入 `quality_policy_audit_logs`，可通过 `GET .../{id}/audit-logs` 查询。`GET /quality-policies/effective` 按时间点和作业属性解析当前生效策略（更具体的范围优先）。

## 验证

运行 `python3 -m pytest -q` 执行服务和领域工具测试，运行 `python3 -m compileall -q app main.py scripts` 检查编译。测试只使用临时 SQLite 数据库，不需要额外服务。
