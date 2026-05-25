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

## 文献翻修工作台 (Web 浏览器界面)

除了命令行界面，项目还提供了一个安静专业的 **双栏 PDF 实时对照翻译工作台**，支持文件拖拽上传、任务状态队列、实时批次进度展示、日志流推送，以及左右双栏原文与中文译文的页码和滚动同步对照阅读。

### 运行启动方式

1. **安装依赖**：
   ```powershell
   pip install -r requirements.txt
   ```
2. **启动后端服务**：
   ```powershell
   uvicorn rsc.web_app:app --host 127.0.0.1 --port 8000
   ```
3. **在浏览器中访问**：
   打开 [http://127.0.0.1:8000/](http://127.0.0.1:8000/) 即可开始使用。

### 本地前端开发

如果您需要修改前端界面：
1. **进入前端目录并安装依赖**：
   ```powershell
   cd my-react-app
   npm install
   ```
2. **启动前端开发服务器**：
   ```powershell
   npm run dev
   ```
3. **在浏览器中访问** [http://localhost:5173/](http://localhost:5173/)，请求会自动被代理至后端的 `8000` 端口。

## 注意事项

- `.env`、原始 PDF、翻译后 PDF、日志、调试脚本和 QA 截图默认不会提交。
- 当前排版回写依赖 Windows 本地字体路径，例如宋体和 Times New Roman。
- 翻译质量和版面适配会受原 PDF 的文本提取结果、分栏结构和图表密度影响。
