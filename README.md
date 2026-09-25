# Ginko

让人格在对话之间延续，让记忆随相遇生长。

Ginko 是一个以百生吟子为初版人格、面向多聊天平台的持续角色智能体项目。
使用 Python 3.13，平台接入采用 NoneBot2 薄适配层，核心与人格、记忆、调度独立。

**当前阶段：0.2.0 开发中。** 已实现统一文本事件、SQLite inbox/outbox、带租约的活动领取、
记忆受众策略、持久预算账本、运行配置检查、OneBot 文本入库、单实例生命周期、有预算约束的模型入口、
结构化人格决策、持久模型尝试审计及配置驱动的聊天运行命令。
已完成测试协议端与真实模型的最小文本往返；服务商账单核对、人格人工评审、候选观察、
自动记忆抽取和自主运行仍待完成。测试协议端不代表真实聊天平台的兼容性已验证。
完整应用已通过合成模型与测试协议端上的 HTTP/WebSocket 故障演练，包括进程崩溃和回执丢失。
这不是已经上线的聊天 Bot，离线 smoke 只验证存储行为，不生成角色回答，也不发送平台消息。

## 开始

```powershell
uv sync --locked
uv run ginko doctor
uv run ginko smoke
uv run ginko persona
```

`doctor` 检查安装和人格资源；`smoke` 使用临时 SQLite 文件完成事件去重、重开数据库恢复待办、
合成预算结算和模拟发送回执。它不需要账号、API Key 或网络。
离线命令的标准输出使用 UTF-8，便于在 Windows / Linux 中重定向并保留中文内容。

0.2.0 的运行配置使用 `config.example.toml` 作为模板，保存到被 Git 忽略的 `config.local.toml`；
设置其引用的凭据环境变量后执行 `uv run ginko check-config config.local.toml`。
检查不连接外部服务，示例预算为零。功能进度见 [路线图](docs/roadmap.md)。

运行后的模型调用可通过以下命令查询，数据库路径取决于配置中的数据目录：

```powershell
uv run ginko model-attempts data/ginko.sqlite3
uv run ginko model-attempts data/ginko.sqlite3 --trace-id <trace_id>
uv run ginko model-attempts data/ginko.sqlite3 <operation_id>
```

查询返回尝试状态、token 用量、费用和结果码，不包含提示词或回答正文。
`reserved` 表示尚无持久结果；可能仍在调用中。取得独占锁的服务重启会将遗留未结算尝试标为
`unknown` 并继续保留预算；单独查询不会触发恢复。`settled` 仅表示费用已结算，
结果码 `accepted` 表示通过模型响应检查，业务校验和平台投递状态需分别查看。

## 结构

```text
src/ginko/
  adapters/          NoneBot / OneBot V11 文本归一化与入库
  core/              平台无关事件、记忆受众、概率换算
  storage/           SQLite 事件、投递、预算
  personas/ginko/    版本化身份、语气与静态知识
  persona.py         人格资源加载
  cli.py             离线检查、投递查询与服务入口
  config.py          运行配置与可信关系授权
  instance.py        本地数据目录的操作系统独占锁
  runtime.py         有界活动 worker 与独占 sender
  gateway.py         显式配置的反向 WebSocket 生命周期
  providers/         单次模型请求、用量校验与预算结算
docs/
  architecture.md    已落实的契约与限制
  roadmap.md         按版本划分的功能范围
  persona/           人格资源与审核说明
  licensing.md       代码、人格与第三方组件的许可说明
tests/               故障、隔离和预算行为测试
```

## 设计约束

- 平台事件的稳定去重键与运行追踪 ID 分开，账号、会话类型、线程纳入命名空间。
- 决策完成与出站意图在一个事务内提交。明确失败和限流有持久终态/等待时间，发送结果未知时停止自动重发，等待回执或核对。
- 同一角色同时领取一个活动。重启后租约到期可重新领取，累计次数和截止时间不重置。
- 记忆先按角色及受众筛选，再检索。账号绑定不自动授予跨会话读取权限；派生记忆取来源权限交集。
- 每次付费尝试必须预留最坏情况费用，日/月硬额度共同约束，未知用量不自动释放。
- 人格是维护者审核的版本资产。角色关系称呼需要可信配置授权，不能由聊天中的自称直接建立。

## 验证

```powershell
uv run ruff check .
uv run ruff format --check .
uv run pytest
uv run ginko smoke
uv build
uv run python scripts/verify_release.py
```

验包命令检查 sdist / wheel 的文件清单、版本和 SHA-256，在源码目录外的新虚拟环境中安装 wheel，
使用锁定的生产依赖执行 `doctor`、`smoke` 和 `persona`，并确认导入来自该虚拟环境。
依赖安装可能访问包索引；这三个应用命令仍不连接平台或模型。结果写入
`dist/verification.json` 与 `dist/SHA256SUMS`。源码包仅包含构建、测试和公共文档所需文件，
不包含原始来源资料、凭据、聊天记录或运行数据。

参见 [架构](docs/architecture.md)、[版本路线图](docs/roadmap.md) 和
[人格资源说明](docs/persona/README.md)。`0.1.0` 离线基础工程已完成；`0.2.0` 正在开发。

项目原创代码采用 [AGPL-3.0-only](LICENSE)，人格素材及第三方组件另见 [许可与来源](docs/licensing.md)。
