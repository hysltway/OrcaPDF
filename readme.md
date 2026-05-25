# auto_PDF_translate

一个基于 Python 的 PDF 翻译工具，用 Google Cloud Translation v2 将英文 PDF 翻译为简体中文，并尽量保留原 PDF 的页面、图片、公式和排版位置。

## 功能

- 批量读取 `original_PDF/` 中的 PDF 文件。
- 调用 Google Cloud Translation v2 Basic API 翻译正文、标题和图表说明。
- 跳过参考文献、公式、表格、侧边注释和部分图内标签，减少误翻译。
- 在原页面上覆盖英文文本并写入中文译文，输出到 `processed_PDF/`。
- 将运行日志写入 `_translate_log.txt`。

## 项目结构

- `rsc/translate_pdf.py`：主脚本，负责提取文本、调用翻译 API、回写 PDF。
- `original_PDF/`：待翻译的原始 PDF。目录内容默认不提交，只保留 `.gitkeep`。
- `processed_PDF/`：翻译后的 PDF 输出目录。生成文件默认不提交，只保留 `.gitkeep`。
- `docs/`：本地参考文档。目录内容默认不提交，只保留 `.gitkeep`。
- `AGENTS.md`：协作与代码修改规则。
- `.gitignore`：忽略本地密钥、PDF 输入输出、日志、截图和调试产物。

## 环境准备

需要 Python 3.12 或兼容版本，并安装依赖：

```powershell
pip install pymupdf requests python-dotenv
```

在仓库根目录创建 `.env`：

```text
GOOGLE_TRANSLATE_API_KEY=你的 Google Cloud Translation API key
```

脚本使用 Cloud Translation v2 Basic API，不使用 v3。

## 使用方法

把需要翻译的 PDF 放入 `original_PDF/`，然后运行：

```powershell
python rsc\translate_pdf.py
```

也可以指定单个 PDF 或目录：

```powershell
python rsc\translate_pdf.py original_PDF\paper.pdf
python rsc\translate_pdf.py original_PDF
```

输出文件会生成到 `processed_PDF/`，文件名格式为：

```text
原文件名_zh-CN.pdf
```

## 注意事项

- `.env`、原始 PDF、翻译后 PDF、日志、调试脚本和 QA 截图默认不会提交。
- 当前排版回写依赖 Windows 本地字体路径，例如宋体和 Times New Roman。
- 翻译质量和版面适配会受原 PDF 的文本提取结果、分栏结构和图表密度影响。
