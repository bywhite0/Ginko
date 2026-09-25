# 许可证与来源范围

Ginko 的原创代码、测试、发布脚本和项目文档采用 **AGPL-3.0-only**。其 SPDX 正式名称为
**GNU Affero General Public License v3.0 only**，见 [SPDX 官方条目](https://spdx.org/licenses/AGPL-3.0-only.html)。
这是仅第 3 版，不是 `AGPL-3.0-or-later`。完整条款见 [LICENSE](../LICENSE)，
文件适用范围见 [NOTICE](../NOTICE)。

许可证文本来自 [GNU 官方 AGPL v3 全文](https://www.gnu.org/licenses/agpl-3.0.txt)，保持原文。
分发代码或二进制时，应按许可证保留通知并提供所要求的对应源码；修改版通过网络与用户交互时，
应按第 13 条向用户提供获取对应源码的机会。依赖包的原有许可证与通知继续适用。

## 人格素材

`src/ginko/personas/ginko/` 包含百生吟子的人格与背景资料，涉及《Love Live! 蓮ノ空女学院
スクールアイドルクラブ》角色内容及维护者提供的人格参考材料。代码许可证不替这些
角色、故事表达、商标或来源文字授予额外权利，也不表示项目拥有原作角色的权利。

这些文件不在上述代码许可范围内，具体声明见
[人格素材权利说明](../LICENSES/LicenseRef-Ginko-Persona.txt)。该说明**不授予人格素材的再分发许可**，
也不能替代对应权利人的授权。原始来源快照、私人研究、聊天记录与私有记忆不进入分发产物。

当前 wheel 和 sdist 包含人格资源，因此包元数据使用
`AGPL-3.0-only AND LicenseRef-Ginko-Persona` 描述不同文件的许可范围，不能把整个归档描述成
全部采用 AGPL 的开源发行物。公开分发包含这些资源的归档前，须确认并记录其分发依据；
没有依据时，应先将相应素材从公开产物移除。源码可见或已通过安装测试都不代替这一确认。

`LicenseRef-Ginko-Persona` 是项目自定义的 SPDX 表达式引用，不是 SPDX 官方许可证列表中的名称，
对应随包提供的素材权利说明；它不改变原创代码的 `AGPL-3.0-only` 选择。

人格加载和维护者审核规则见 [人格资源说明](persona/README.md)。

## 第三方依赖

Ginko 的源码包和 wheel 不内嵌任何依赖的源码或二进制；安装时由包管理器按 `uv.lock` 分别取得，
各依赖的原有许可证与通知随其自身分发包保留并继续适用。

直接运行依赖及其许可声明如下。传递依赖、开发依赖和构建工具由锁文件与构建后端决定，
版本随锁文件更新，不在本文逐项记录；完整清单以 `uv.lock` 为准。

| 包 | 用途 | 许可声明 |
|---|---|---|
| pydantic | 配置与数据校验 | MIT |
| nonebot2 | 平台接入框架 | MIT |
| nonebot-adapter-onebot | OneBot V11 适配 | MIT |
| httpx | 模型 HTTP 客户端 | BSD-3-Clause |
