# 🐋 OrcaPDF (Bilingual PDF Workbench)

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.12+-blue.svg?style=for-the-badge&logo=python&logoColor=white" alt="Python Version" />
  <img src="https://img.shields.io/badge/FastAPI-0.110+-009688.svg?style=for-the-badge&logo=fastapi&logoColor=white" alt="FastAPI" />
  <img src="https://img.shields.io/badge/React-18.3+-61DAFB.svg?style=for-the-badge&logo=react&logoColor=black" alt="React" />
  <img src="https://img.shields.io/badge/Vite-5.2+-646CFF.svg?style=for-the-badge&logo=vite&logoColor=white" alt="Vite" />
</p>

---

**OrcaPDF** 是一款专业级的 **双栏 PDF 实时对照翻译工作台**，旨在提供极致的学术文献阅读与翻译体验。本项目致力于打造一个本地化、高性能的学术翻译方案，完美替代“小绿鲸”等商业 PDF 翻译工具。

通过集成强大的 **Google Cloud Translation API** 与智能排版算法，OrcaPDF 能够在翻译文本的同时，**最大程度保留原始 PDF 的页面结构、多栏排版、数学公式、高精图表与图注位置**，让学术阅读流畅无阻。

---

## ✨ 核心亮点

*   🎨 **版面级还原 (Layout-Preserved)**
    智能检测分栏、标题、段落和图注，在原页面上精准覆盖英文并回写中文译文，保留字体粗细、大小及排版美感。
*   📊 **学术级降噪 (Academic Noise Reduction)**
    自动识别并跳过复杂的 LaTeX 公式、纯数据表格、参考文献（References）以及页面边栏页眉，避免翻译错乱，同时大幅节省 API 翻译额度。
*   🚀 **双栏同步阅读器 (Bilingual Workbench)**
    基于 React + react-pdf 打造的高清阅读器，支持左右双栏**滚动同步、页码同步**，原文与译文实时对照，查阅无缝衔接。
*   ⚡ **异步高并发任务队列**
    后端基于 FastAPI + BackgroundTasks 构建，支持多文件拖拽上传、异步排队处理、WebSocket/SSE 任务状态与**实时日志流推送**。

---

## 🏗️ 项目架构

```text
auto_PDF_translate/
├── rsc/                  # 后端核心源码
│   ├── translate_pdf.py  # PDF 文本提取、API 翻译及 PDF 排版重写引擎
│   └── web_app.py        # FastAPI 后端服务、任务管理及静态资源托管
├── my-react-app/         # 前端 React 源码 (Vite + TypeScript + TailwindCSS)
├── original_PDF/         # 待翻译的原始 PDF 目录
├── processed_PDF/        # 翻译完成的 PDF 输出目录
├── docs/                 # 本地开发参考文档与 API 规范
└── readme.md             # 项目指南
```

---

## ⚡ 快速启动

### 1. 环境准备

确保您的系统中已安装 Python 3.12 或兼容版本，以及 Node.js 环境（用于前端开发）。

#### 安装后端依赖
```powershell
pip install -r requirements.txt
```

#### 配置与接口切换 (.env)
在项目根目录下创建一个名为 `.env` 的文件，填入对应密钥，并配置翻译提供商：

```ini
# 翻译服务提供商选择: 'siliconflow' (默认) 或 'google'
TRANSLATE_PROVIDER=siliconflow

# SiliconFlow 硅基流动 API 密钥
siliconflow_TRANSLATE_API_KEY=your_siliconflow_api_key

# Google Cloud Translation v2 API 密钥
GOOGLE_TRANSLATE_API_KEY=your_google_cloud_translation_api_key
```

> 
> *   **SiliconFlow 模式** (推荐/默认)：调用百亿/千亿级专业机器翻译大模型（默认使用 `tencent/Hunyuan-MT-7B`，可大幅提升学术句式与专业词汇翻译质量）。
> - **Google 模式**：调用经典的 Google Cloud 基础版翻译 API，运行稳定快捷。
> - 所有的 API Key 均保存在本地 `.env` 文件中，已被 `.gitignore` 排除.

---

### 2. 启动文献翻修工作台 (推荐)

这是最直观的使用方式，提供完整的可视化上传、队列管理与对照阅读界面。

```powershell
uvicorn rsc.web_app:app --host 127.0.0.1 --port 8000
```
启动后，在浏览器中访问 [http://127.0.0.1:8000/](http://127.0.0.1:8000/) 即可开始使用。

---

### 3. 命令行极客模式 (CLI)

如果您更喜欢终端操作，可以直接使用命令行脚本进行批量或单文件翻译：

```powershell
# 自动翻译 original_PDF/ 下的所有 PDF 文件
python rsc/translate_pdf.py

# 翻译指定文件
python rsc/translate_pdf.py original_PDF/paper.pdf

# 翻译整个指定目录
python rsc/translate_pdf.py original_PDF/

# 调整中文版面行高 (默认 1.0，可根据字体适配微调，例如 1.15)
python rsc/translate_pdf.py --line-height 1.15 original_PDF/paper.pdf
```

翻译完成后，生成的中文 PDF 将会自动保存至 `processed_PDF/` 目录，命名格式为 `[原文件名]_zh-CN.pdf`。

---

## 🛠️ 前端二次开发

如果您想要定制或优化双栏对照界面的 UI：

1. **进入前端项目目录并安装依赖**：
   ```powershell
   cd my-react-app
   npm install
   ```
2. **启动本地开发服务器**：
   ```powershell
   npm run dev
   ```
3. **开始开发**：
   打开浏览器访问 [http://localhost:5173/](http://localhost:5173/)。前端已配置代理，所有的 API 请求和任务流会自动转发至后端的 `8000` 端口。

---

## ⚠️ 注意事项

*   **本地字体依赖**：PDF 的中文回写依赖本地系统字体，脚本默认会尝试读取 Windows 系统的常见字体路径（如宋体 `simsun.ttc`、Times New Roman 等）。若在 Linux/macOS 环境下运行，可能需要配置相应的系统字体。
*   **版面适配限制**：翻译效果和版面还原度取决于原始 PDF 的文本可提取性。对于双栏公式密集、扫描件或含有大量重合图表组件的 PDF，还原排版可能会有一定的偏移，建议通过工作台配合原文对照阅读。
*   **隐私与安全**：您的敏感配置（如 `.env` 中的 API Key）、待翻译的 PDF 及生成的译文均保存在本地，默认已被 `.gitignore` 排除，绝不上传至任何第三方服务器。

---

## 🙏 致谢

本项目的核心 PDF 解析、重构渲染与排版回写机制，深受开源项目 **[OpenDataLoader PDF](https://github.com/opendataloader-project/opendataloader-pdf)** 的启发与技术积累。

在此对 [opendataloader-project](https://github.com/opendataloader-project) 团队在 PDF 文献数字化及结构化解析领域做出的杰出贡献与开源精神致以最诚挚的敬意和谢意！ ❤️

---

## 📄 开源协议

本项目采用 [MIT License](LICENSE) 许可协议。
