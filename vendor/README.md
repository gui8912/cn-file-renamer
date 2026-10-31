# 内置第三方组件

本目录只包含运行时必需的第三方代码/字典，避免用户执行 `pip install`。
所有组件均按 MIT 许可随本工具分发，原始版权与许可原文保留在同目录。

## zhconv 1.2.2

- 用途：繁简转换与异体字归一。
- 来源：https://github.com/gumblex/zhconv
- PyPI：https://pypi.org/project/zhconv/1.2.2/
- 作者：Dingyuan Wang
- 许可：MIT，见 `LICENSE-zhconv.txt`。
- 说明：`zhconv.py`、`zhcdict.json`、`__init__.py`、`__main__.py` 解包自官方 wheel。
  其中 `zhcdict.json` 由 MediaWiki LanguageConverter 转换表派生。
- 本地修改（已标注在源码中）：`loaddict()` 读取默认字典后显式关闭 pkgutil
  资源句柄，修复上游 1.2.2 的 ResourceWarning；不改变转换行为。
- 注意：上游新版本可能改为其他许可；升级前必须重新核对，不要直接覆盖本目录。

## 修改记录

本项目的 `core/text_norm.py` 是独立封装，不修改上述库源码。
若升级第三方组件，必须同时更新本文件中的版本、来源和许可证。
