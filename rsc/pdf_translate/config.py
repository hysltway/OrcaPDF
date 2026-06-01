from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCE_DIR = ROOT / "original_PDF"
OUTPUT_DIR = ROOT / "processed_PDF"
LOG_PATH = ROOT / "_translate_log.txt"

SILICONFLOW_CHAT_URL = "https://api.siliconflow.cn/v1/chat/completions"
SILICONFLOW_MODEL = "tencent/Hunyuan-MT-7B"
TRANSLATE_API_KEY_ENV = "siliconflow_TRANSLATE_API_KEY"
TYPESETTING_LINE_HEIGHT_ENV = "PDF_TRANSLATE_LINE_HEIGHT"
TARGET_LANGUAGE = "zh-CN"
CJK_REGULAR_FONT = "C:/Windows/Fonts/STSONG.TTF"
CJK_BOLD_FONT = "C:/Windows/Fonts/simhei.ttf" if Path("C:/Windows/Fonts/simhei.ttf").exists() else CJK_REGULAR_FONT
TIMES_FONT = "C:/Windows/Fonts/times.ttf"
TIMES_BOLD_FONT = "C:/Windows/Fonts/timesbd.ttf"
TIMES_ITALIC_FONT = "C:/Windows/Fonts/timesi.ttf"
TIMES_BOLD_ITALIC_FONT = "C:/Windows/Fonts/timesbi.ttf"
FONT_DIR = str(Path(CJK_REGULAR_FONT).parent)
FONT_FACE_CSS = f"""
@font-face {{ font-family: CjkRegularLocal; src: url({Path(CJK_REGULAR_FONT).name}); }}
@font-face {{ font-family: CjkBoldLocal; src: url({Path(CJK_BOLD_FONT).name}); }}
@font-face {{ font-family: TimesLocal; src: url({Path(TIMES_FONT).name}); }}
@font-face {{ font-family: TimesBoldLocal; src: url({Path(TIMES_BOLD_FONT).name}); }}
@font-face {{ font-family: TimesItalicLocal; src: url({Path(TIMES_ITALIC_FONT).name}); }}
@font-face {{ font-family: TimesBoldItalicLocal; src: url({Path(TIMES_BOLD_ITALIC_FONT).name}); }}
"""
MIN_FONT_SIZE = 4.5  # 翻译回写 PDF 时的最小字号限制（防止文字缩得过小无法阅读）
DEFAULT_TEXT_LINE_HEIGHT = 1.2  # 排版时默认的文本行高
FAST_CJK_FONT_NAME = "CjkRegularFast"
FAST_CJK_BOLD_FONT_NAME = "CjkBoldFast"
MAX_CONCURRENT_BATCHES = 128  # 允许并发提交到大模型翻译的批次（Batch）上限
MAX_BATCH_ITEMS = 1  # 每个翻译批次中包含的文本单元（Unit）数量最大值
MAX_BATCH_CHARS = 12000  # 每个翻译批次所允许的最大字符长度限制
MAX_UNIT_CHARS = 2500  # 单个合并文本单元（Unit）的最大字符长度上限
MAX_CONCURRENT_PDFS = 4  # 允许同时并发处理的 PDF 文件任务上限
TRANSLATION_ATTEMPTS = 3  # 单个翻译请求失败后的最大重试次数
TRANSLATION_TIMEOUT = 60  # 单个翻译请求的超时时间（秒）
RETRY_SLEEP_SECONDS = 0.5  # 翻译失败重试前的休眠等待时间（秒）
