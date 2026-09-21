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

## 运行依赖

以下版本以 `uv.lock` 为准，核对日期为 2026-09-22。核对依据是对应 Python 分发包的
`METADATA` 与随包许可证全文；各包名链接到固定版本的公开索引页。

| 包 | 锁定版本 | 许可证 |
|---|---|---|
| [pydantic](https://pypi.org/project/pydantic/2.13.5/) | 2.13.5 | MIT |
| [pydantic-core](https://pypi.org/project/pydantic-core/2.46.5/) | 2.46.5 | MIT |
| [annotated-types](https://pypi.org/project/annotated-types/0.8.0/) | 0.8.0 | MIT |
| [typing-inspection](https://pypi.org/project/typing-inspection/0.4.4/) | 0.4.4 | MIT |
| [typing-extensions](https://pypi.org/project/typing-extensions/4.16.0/) | 4.16.0 | PSF-2.0 |

`typing-extensions` 的许可证文件还保留 Python 历史许可条款；重新分发该依赖时须保留完整上游文件，
不能用表中的 SPDX 标识替换其通知。`pydantic` 是直接依赖，其余为传递依赖。
Ginko 的源码包和 wheel 不内嵌这些依赖的源码或二进制，安装时由包管理器分别取得。

## 开发与构建工具

开发依赖同样由 `uv.lock` 锁定，但不属于 Ginko 的生产依赖。

| 包 | 锁定版本 | 许可证 |
|---|---|---|
| [pytest](https://pypi.org/project/pytest/9.1.1/) | 9.1.1 | MIT |
| [ruff](https://pypi.org/project/ruff/0.16.8/) | 0.16.8 | MIT |
| [iniconfig](https://pypi.org/project/iniconfig/2.3.0/) | 2.3.0 | MIT |
| [pluggy](https://pypi.org/project/pluggy/1.6.0/) | 1.6.0 | MIT |
| [packaging](https://pypi.org/project/packaging/26.3/) | 26.3 | Apache-2.0 OR BSD-2-Clause |
| [pygments](https://pypi.org/project/pygments/2.21.0/) | 2.21.0 | BSD-2-Clause |
| [colorama](https://pypi.org/project/colorama/0.4.6/) | 0.4.6 | BSD-3-Clause；仅 Windows 的 pytest 依赖 |

构建后端声明为 `hatchling>=1.27,<2`，构建环境的依赖不由当前 `uv.lock` 锁定。
以下是本次核对的构建工具样本，不能据此认为未来构建会使用相同版本。

| 包 | 核对版本 | 许可证 |
|---|---|---|
| [hatchling](https://pypi.org/project/hatchling/1.32.4/) | 1.32.4 | MIT |
| [pathspec](https://pypi.org/project/pathspec/1.1.1/) | 1.1.1 | MPL-2.0 |
| [tomlkit](https://pypi.org/project/tomlkit/0.15.1/) | 0.15.1 | MIT |
| [trove-classifiers](https://pypi.org/project/trove-classifiers/2026.9.21.13/) | 2026.9.21.13 | Apache-2.0 |
| [editables](https://pypi.org/project/editables/0.6/) | 0.6 | MIT；用于 editable 构建 |

构建链还使用上表中的 `packaging` 和 `pluggy`。`pathspec` 的 MPL-2.0 条款不因工具被用于构建
而覆盖 Ginko 的构建输出；若今后复制、修改或随其他交付物分发其代码，须另行履行相应条款。

## 核对边界

已核对的依赖许可未发现与 Ginko 原创代码采用 AGPL-3.0-only 冲突的条件。本次检查覆盖 Python
分发包声明和许可证，没有逐项审计上游原生二进制内的全部组件；这也不是对任意未来依赖的结论。
依赖版本、复制代码范围或交付方式改变时，需要重新核对，并保留所分发组件要求的许可证及通知。

当前没有复制或打包 NoneBot、OneBot 适配器或 NapCat 实现；它们尚未进入依赖和支持组合，
后续接入时按实际组件与版本核对，不能用接口兼容代替许可确认。
