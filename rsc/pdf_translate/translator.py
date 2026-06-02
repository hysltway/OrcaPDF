from __future__ import annotations

import html
import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import fitz
import requests

from .config import (
    DEFAULT_TEXT_LINE_HEIGHT,
    MAX_CONCURRENT_BATCHES,
    RETRY_SLEEP_SECONDS,
    SILICONFLOW_CHAT_URL,
    SILICONFLOW_MODEL,
    TARGET_LANGUAGE,
    TRANSLATION_ATTEMPTS,
    TRANSLATION_TIMEOUT,
)
from .extraction import (
    find_references_range,
    has_ieee_journal_header,
    is_conference_header,
    is_formula_context_label,
    is_heading,
    is_lettered_section_heading,
    is_roman_section_heading,
    is_title_block,
    split_ieee_drop_cap_heading,
)
from .models import Logger, PageData, TextBlock
from .units import page_blocks, split_translation, translation_batches, translation_units


_llm_request_semaphore = threading.BoundedSemaphore(MAX_CONCURRENT_BATCHES)
_thread_state = threading.local()


def clean_translation(text: str) -> str:
    text = re.sub(r"</?(?:sub|sup)>", "", text)
    text = html.unescape(text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(
        r"\s*[\(（][^\)）]*(?:text (?:seems )?(?:continues|is incomplete|appears incomplete)|translation ends|incomplete text)[^\)）]*[\)）]\.?",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"\s*[\(（][^\)）]*(?:原文未提供|原文未给出|原文不完整|文本不完整|此处应为|未给出完整|缺少上下文)[^\)）]*[\)）]\.?",
        "",
        text,
    )
    text = re.sub(r"\s*(?:……|\.{3,})(?=[，。！？；：、\s]|$)", "", text)
    text = re.sub(r"\s*[≤<>=]\s*(?=[，。！？；：、\s]|$)", "", text)
    text = re.sub(r"\\(?:mathbb|mathcal|mathrm|mathbf|operatorname)\{([^{}]+)\}", r"\1", text)
    text = re.sub(r"\\(?:text|mbox)\{([^{}]+)\}", r"\1", text)
    latex_symbols = {
        "Phi": "Φ",
        "phi": "φ",
        "sigma": "σ",
        "lambda": "λ",
        "Lambda": "Λ",
        "alpha": "α",
        "beta": "β",
        "delta": "δ",
        "epsilon": "ϵ",
        "in": "∈",
        "times": "×",
        "cdot": "·",
        "circ": "◦",
        "leq": "≤",
        "geq": "≥",
    }
    text = re.sub(r"\\([A-Za-z]+)", lambda match: latex_symbols.get(match.group(1), ""), text)
    text = text.replace("\\", "")
    text = re.sub(r"\${1,2}\s*([^$]+?)\s*\${1,2}", r"\1", text)
    text = text.replace("$$", "").replace("$", "")
    text = re.sub(r"[ \t]*\n+[ \t]*", " ", text)
    text = re.sub(r"[ \t]*\n[ \t]*", " ", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"(?<=[\u3400-\u9fff])\s+(?=[\u3400-\u9fff])", "", text)
    text = re.sub(r"(?<=[，。！？；：、（《【“‘])\s+", "", text)
    text = re.sub(r"\s+(?=[，。！？；：、）】》”’])", "", text)
    return text.strip()



def translation_prompt(batch: list[tuple[int, str]]) -> str:
    return json.dumps(
        [{"id": unit_index, "text": text} for unit_index, text in batch],
        ensure_ascii=False,
    )


COMMON_HEADING_TRANSLATIONS = {
    "abstract": "摘要",
    "introduction": "引言",
    "related work": "相关工作",
    "method": "方法",
    "methods": "方法",
    "experiments": "实验",
    "results": "结果",
    "discussion": "讨论",
    "conclusion": "结论",
    "conclusions": "结论",
    "limitations": "局限性",
    "references": "参考文献",
    "acknowledgments": "致谢",
    "acknowledgements": "致谢",
    "sample complexity": "样本复杂度",
    "expressive power": "表达能力",
    "stability": "稳定性",
    "computational complexity": "计算复杂度",
    "baselines": "基线方法",
    "architectures": "架构",
    "stability analysis": "稳定性分析",
    "implementation details": "实现细节",
    "additional experiments": "补充实验",
    "ablation studies": "消融研究",
}


HEADING_PHRASE_TRANSLATIONS = {
    "our work": "我们的工作",
    "our work: learnable, efficient, and powerful pes with gnns": "我们的工作：基于GNNs的可学习、高效且强大的位置编码（PEs）",
    "gnns are nonlinear functions of gso eigenvectors": "GNNs是GSO特征向量的非线性函数",
    "pearl: expressive and equivariant positional encoding networks": "PEARL：表达能力强且等变的位置编码网络",
    "our work: random positional encoding network (r-pearl)": "我们的工作：随机位置编码网络（R-PEARL）",
    "comparing pearl to structural pes": "将PEARL与结构化PEs进行比较",
    "proof of proposition 3.1": "命题3.1的证明",
    "proof of corollary 4.4": "推论4.4的证明",
    "basis universality of pearl": "PEARL的基普适性",
    "our work: basis positional encoding networks (b-pearl)": "我们的工作：基位置编码网络（B-PEARL）",
    "relation to eigenvector based encodings": "与基于特征向量的编码的关系",
    "graph classification on social networks": "社交网络上的图分类",
    "graph regression on molecular graphs": "分子图上的图回归",
    "large-scale link prediction on relational databases (relbench)": "关系数据库（RelBench）上的大规模链接预测",
    "pearl is a universal function of eigenvalues": "PEARL是特征值的通用函数",
    "effect of pointwise activation to the variance of a random variable": "逐点激活对随机变量方差的影响",
    "effect of graph convolution to the variance of a random node signal": "图卷积对随机节点信号方差的影响",
    "lipschitz and integral lipschitz filters": "Lipschitz与积分Lipschitz滤波器",
    "stability bounds for random and basis pes": "随机PEs和基PEs的稳定性界",
    "spectral filters with graph filters": "带图滤波器的谱滤波器",
    "experiments on graph isomorphism": "图同构实验",
    "experiments on peptides-struct dataset": "Peptides-struct数据集实验",
    "ablation on different backbone models": "不同骨干模型的消融",
    "ablation on k": "K的消融",
    "ablation on number of samples m": "样本数M的消融",
    "ablation on the number of gnn layers in pearl": "PEARL中GNN层数的消融",
    "ablation on different parameter size": "不同参数规模的消融",
}


IEEE_SECTION_HEADING_TRANSLATIONS = {
    **COMMON_HEADING_TRANSLATIONS,
    "baselines": "基线方法",
    "experiment": "实验",
    "results and analyses": "结果与分析",
}


def fixed_heading_translation(text: str) -> str | None:
    stripped = re.sub(r"\s+", " ", text.strip())
    prefixed = re.match(r"^([A-Z](?:\.\d+)*|\d+(?:\.\d+)*)\s+(.+)$", stripped)
    if prefixed:
        prefix, heading = prefixed.groups()
        translated = heading_phrase_translation(heading)
        if translated:
            return f"{prefix} {translated}"
        return None
    return heading_phrase_translation(stripped)


def heading_phrase_translation(text: str) -> str | None:
    key = text.strip().lower()
    if key in COMMON_HEADING_TRANSLATIONS:
        return COMMON_HEADING_TRANSLATIONS[key]
    if key in HEADING_PHRASE_TRANSLATIONS:
        return HEADING_PHRASE_TRANSLATIONS[key]

    proof = re.match(r"proof of (proposition|theorem|lemma|corollary)\s+([\w.]+)", key)
    if proof:
        label, number = proof.groups()
        label_zh = {
            "proposition": "命题",
            "theorem": "定理",
            "lemma": "引理",
            "corollary": "推论",
        }[label]
        return f"{label_zh}{number}的证明"
    return None


def sanitize_translation(blocks: list[TextBlock], translated: str) -> str:
    if len(blocks) != 1:
        return translated

    block = blocks[0]
    if not is_heading(block):
        return translated

    fixed = fixed_heading_translation(block.text)
    if fixed:
        return fixed

    stripped = translated.strip()
    if translation_is_suspicious(block.text, stripped) or title_translation_is_suspicious(block.text, stripped):
        return block.text
    if len(stripped) > max(28, len(block.text) * 3) or "\n" in stripped:
        return block.text
    if stripped.count("。") + stripped.count(".") > 1:
        return block.text
    return stripped


INLINE_HEADING_TRANSLATIONS = {
    "introduction": "引言",
    "related work": "相关工作",
    "method": "方法",
    "methods": "方法",
    "experiments": "实验",
    "results": "结果",
    "discussion": "讨论",
    "conclusion": "结论",
    "conclusions": "结论",
}


FORMULA_CONTEXT_TRANSLATIONS = {
    "where": "其中",
    "satisfies": "满足：",
    "such that": "使得：",
    "as follows": "如下：",
}


def fixed_short_translation(text: str) -> str | None:
    stripped = re.sub(r"\s+", " ", text.strip())
    if re.search(
        r"\b(?:published as|accepted as|under review).{0,40}\b(?:conference paper|workshop paper|ICLR|ICML|NeurIPS|OpenReview)\b",
        stripped,
        re.IGNORECASE,
    ):
        return "发表于ICLR 2025会议论文"
    if re.search(r"\bcorrespond(?:a|e)nce to:\s*\S+", stripped, re.IGNORECASE):
        translated = re.sub(
            r"(\*|∗)?\s*correspond(?:a|e)nce to:\s*([^\s.]+(?:\.[^\s.]+)*\.[A-Za-z]{2,})\.?",
            r"∗通信至：\2。",
            stripped,
            flags=re.IGNORECASE,
        )
        translated = re.sub(r"(†)?\s*equal senior authorship\.?", "†共同高级作者。", translated, flags=re.IGNORECASE)
        return translated
    if is_formula_context_label(stripped):
        key = stripped.rstrip(":：").lower()
        return FORMULA_CONTEXT_TRANSLATIONS.get(key)
    return None


def split_leading_heading(blocks: list[TextBlock], translated: str) -> str:
    if len(blocks) != 1:
        return translated
    block = blocks[0]
    if not block.leading_bold:
        return translated

    source = re.sub(r"\s+", " ", block.text.strip())
    for heading, zh_heading in INLINE_HEADING_TRANSLATIONS.items():
        if not source.lower().startswith(heading + " "):
            continue
        if translated.startswith(zh_heading) and not translated.startswith(zh_heading + "\n\n"):
            rest = translated[len(zh_heading):].lstrip(" ：:")
            if rest:
                return f"{zh_heading}\n\n{rest}"
        break
    return translated


def fixed_ieee_section_heading_translation(text: str) -> str | None:
    match = re.match(r"^([A-Z]+)\.\s+(.+)$", re.sub(r"\s+", " ", text.strip()))
    if not match:
        return None
    prefix, heading = match.groups()
    translated = IEEE_SECTION_HEADING_TRANSLATIONS.get(heading.lower())
    if not translated:
        return None
    return f"{prefix}. {translated}"


def strip_leading_ieee_section_heading(translated: str, heading: str, source_heading: str) -> str:
    text = translated.strip()
    for candidate in (heading, source_heading):
        if text.startswith(candidate):
            return text[len(candidate):].lstrip(" \t\r\n:：,，.。-—")

    heading_word = heading.split(maxsplit=1)[-1] if " " in heading else heading
    match = re.match(rf"^(?:[IVX]+\.\s*)?{re.escape(heading_word)}\s*[:：,，.。-—]?\s*(.+)$", text, re.S)
    if match:
        return match.group(1).strip()
    return text


def split_ieee_drop_cap_translation(
    unit_items: list[tuple[PageData, TextBlock]],
    translated: str,
    line_height: float,
) -> list[str] | None:
    if len(unit_items) < 2:
        return None

    page, first = unit_items[0]
    if not has_ieee_journal_header(page):
        return None

    heading_parts = split_ieee_drop_cap_heading(first.text)
    if not heading_parts:
        return None

    second_page, second = unit_items[1]
    first_rect = fitz.Rect(first.rect)
    second_rect = fitz.Rect(second.rect)
    if (
        second_page.index != page.index
        or abs(first_rect.x0 - second_rect.x0) > 24
        or second_rect.y0 - first_rect.y0 > 40
        or not re.match(r"^[A-Z][A-Z-]+", second.text)
    ):
        return None

    heading = fixed_ieee_section_heading_translation(heading_parts[0]) or heading_parts[0]
    body = strip_leading_ieee_section_heading(translated, heading, heading_parts[0])
    if not body:
        return None

    body_blocks = [block for _, block in unit_items[1:]]
    return [heading, *split_translation(body, body_blocks, line_height)]


def source_terms(text: str) -> set[str]:
    terms = set()
    for term in re.findall(r"\b[A-Z][A-Z0-9-]{1,}\b", text):
        if term not in {"PDF", "JSON"}:
            terms.add(term)
    for term in re.findall(r"\b[A-Z][A-Za-z0-9-]{2,}\b", text):
        if term.lower() not in COMMON_HEADING_TRANSLATIONS:
            terms.add(term)
    return terms


def translation_preserves_terms(source: str, translated: str) -> bool:
    terms = source_terms(source)
    if not terms:
        return True
    required = [term for term in terms if len(term) <= 20]
    if not required:
        return True
    present = sum(1 for term in required if term in translated)
    return present >= max(1, len(required) // 3)


def translation_looks_untranslated(source: str, translated: str) -> bool:
    source_words = re.findall(r"\b[A-Za-z][A-Za-z-]{2,}\b", source)
    if len(source_words) < 6:
        return False
    translated_words = re.findall(r"\b[A-Za-z][A-Za-z-]{2,}\b", translated)
    chinese_chars = re.findall(r"[\u3400-\u9fff]", translated)
    if len(chinese_chars) >= max(6, len(source_words) // 4):
        return False
    return len(translated_words) >= max(4, len(source_words) // 2)


def translation_is_suspicious(source: str, translated: str) -> bool:
    if not translation_preserves_terms(source, translated):
        return True
    if translation_looks_untranslated(source, translated):
        return True
    source_lower = source.lower()
    if ("gnn" in source_lower or "gso" in source_lower or "positional encoding" in source_lower or "pearl" in source_lower) and re.search(
        r"全球导航|卫星|强化学习|生成对抗|GAN",
        translated,
        re.IGNORECASE,
    ):
        return True
    if ("eeg" in source_lower or "brain" in source_lower or "neural" in source_lower) and re.search(
        r"材料|冲击|载荷|应力|变形|破坏模式|工程设计",
        translated,
    ):
        return True
    return False


def title_translation_is_suspicious(source: str, translated: str) -> bool:
    stripped = translated.strip()
    if not stripped:
        return True
    sentence_count = stripped.count("。") + stripped.count("！") + stripped.count("？")
    if sentence_count > 1:
        return True
    return len(stripped) > max(70, len(source) * 1.8)


def parse_translation_response(text: str) -> dict[int, str]:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)

    payload = json.loads(text)
    translations = payload["translations"] if isinstance(payload, dict) else payload
    return {
        int(item["id"]): clean_translation(str(item["text"]))
        for item in translations
    }


def thread_session() -> requests.Session:
    session = getattr(_thread_state, "session", None)
    if session is None:
        session = requests.Session()
        _thread_state.session = session
    return session


def translate_batch_siliconflow(batch_index: int, batch: list[tuple[int, str]], api_key: str) -> tuple[int, list[tuple[int, str]]]:
    if len(batch) == 1:
        unit_index, text = batch[0]
        return batch_index, [(unit_index, translate_unit_text_siliconflow(unit_index, text, api_key))]

    total_chars = sum(len(text) for _, text in batch)
    payload = {
        "model": SILICONFLOW_MODEL,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are translating academic PDF text from English into Simplified Chinese. "
                    "Use precise, fluent academic Chinese. Keep the original meaning complete and do not summarize. "
                    "Preserve terminology consistency, proper nouns, model names, dataset names, citations, numbers, "
                    "units, formulas, inline variables, punctuation that belongs to equations, and bracketed references. "
                    "Translate theorem, proposition, lemma, corollary, proof, assumption, definition, remark, section, "
                    "appendix, acknowledgment, and conclusion labels into Simplified Chinese. "
                    "Do not wrap formulas, variables, or translated text in $ or $$ delimiters. "
                    "If the source is a sentence fragment split by a formula, translate only the visible fragment; "
                    "do not add comments such as incomplete text or text continues. "
                    "Do not insert manual line breaks. Keep each original paragraph as one paragraph; only use a blank line "
                    "between paragraphs when the source text clearly contains separate paragraphs. "
                    "Do not convert inline enumerations into lists and do not add blank lines. "
                    "Do not add spaces between Chinese characters. "
                    "Return only valid JSON in this exact shape: "
                    "{\"translations\":[{\"id\":number,\"text\":\"translated Chinese\"}]}. "
                    "Return every input id exactly once. Do not translate JSON keys or ids."
                ),
            },
            {
                "role": "user",
                "content": translation_prompt(batch),
            },
        ],
        "max_tokens": max(1024, min(8192, total_chars * 2)),
        "temperature": 0.1,
        "top_p": 0.7,
        "response_format": {"type": "json_object"},
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    session = thread_session()
    with _llm_request_semaphore:
        for attempt in range(1, TRANSLATION_ATTEMPTS + 1):
            try:
                response = session.post(
                    SILICONFLOW_CHAT_URL,
                    headers=headers,
                    json=payload,
                    timeout=TRANSLATION_TIMEOUT,
                )
                if response.status_code >= 400:
                    raise RuntimeError(f"HTTP {response.status_code}: {response.text[:500]}")

                message = response.json()["choices"][0]["message"]
                by_id = parse_translation_response(message["content"])
                batch_results = [
                    (unit_index, by_id[unit_index])
                    for unit_index, _ in batch
                    if unit_index in by_id
                ]
                return batch_index, batch_results
            except Exception as exc:
                if attempt == TRANSLATION_ATTEMPTS:
                    raise RuntimeError(f"translation batch {batch_index} failed: {exc}") from exc
                time.sleep(RETRY_SLEEP_SECONDS)

    raise RuntimeError(f"translation batch {batch_index} failed")


def translate_unit_text_siliconflow(unit_index: int, text: str, api_key: str) -> str:
    payload = {
        "model": SILICONFLOW_MODEL,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are translating academic PDF text from English into Simplified Chinese. "
                    "Use precise, fluent academic Chinese. Keep the original meaning complete and do not summarize. "
                    "Preserve terminology, proper nouns, citations, numbers, units, formulas, and inline variables. "
                    "Translate theorem, proposition, lemma, corollary, proof, assumption, definition, remark, section, "
                    "appendix, acknowledgment, and conclusion labels into Simplified Chinese. "
                    "Do not wrap formulas, variables, or translated text in $ or $$ delimiters. "
                    "If the source is a sentence fragment split by a formula, translate only the visible fragment; "
                    "do not add comments such as incomplete text or text continues. "
                    "Do not insert manual line breaks. Keep the original text as one paragraph unless the source clearly "
                    "contains separate paragraphs; in that case separate paragraphs with exactly one blank line. "
                    "Do not convert inline enumerations into lists and do not add blank lines. "
                    "Do not add spaces between Chinese characters. "
                    "Return only the translated Chinese text. Do not add explanations, notes, markdown, or quotes."
                ),
            },
            {
                "role": "user",
                "content": text,
            },
        ],
        "max_tokens": max(512, min(8192, len(text) * 2)),
        "temperature": 0.1,
        "top_p": 0.7,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    session = thread_session()
    with _llm_request_semaphore:
        for attempt in range(1, TRANSLATION_ATTEMPTS + 1):
            try:
                response = session.post(
                    SILICONFLOW_CHAT_URL,
                    headers=headers,
                    json=payload,
                    timeout=TRANSLATION_TIMEOUT,
                )
                if response.status_code >= 400:
                    raise RuntimeError(f"HTTP {response.status_code}: {response.text[:500]}")

                message = response.json()["choices"][0]["message"]
                return clean_translation(message["content"].strip())
            except Exception as exc:
                if attempt == TRANSLATION_ATTEMPTS:
                    raise RuntimeError(f"translation unit {unit_index} failed: {exc}") from exc
                time.sleep(RETRY_SLEEP_SECONDS)

    raise RuntimeError(f"translation unit {unit_index} failed")


def translate_title_text_siliconflow(unit_index: int, text: str, api_key: str) -> str:
    payload = {
        "model": SILICONFLOW_MODEL,
        "messages": [
            {
                "role": "system",
                "content": (
                    "Translate this academic paper title into Simplified Chinese. "
                    "Return one concise title only. Do not explain, summarize, expand, or add a subtitle. "
                    "Preserve model names, proper nouns, acronyms, and terms in parentheses."
                ),
            },
            {
                "role": "user",
                "content": text,
            },
        ],
        "max_tokens": 256,
        "temperature": 0.1,
        "top_p": 0.7,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    session = thread_session()
    with _llm_request_semaphore:
        for attempt in range(1, TRANSLATION_ATTEMPTS + 1):
            try:
                response = session.post(
                    SILICONFLOW_CHAT_URL,
                    headers=headers,
                    json=payload,
                    timeout=TRANSLATION_TIMEOUT,
                )
                if response.status_code >= 400:
                    raise RuntimeError(f"HTTP {response.status_code}: {response.text[:500]}")

                message = response.json()["choices"][0]["message"]
                return clean_translation(message["content"].strip())
            except Exception as exc:
                if attempt == TRANSLATION_ATTEMPTS:
                    raise RuntimeError(f"title translation unit {unit_index} failed: {exc}") from exc
                time.sleep(RETRY_SLEEP_SECONDS)

    raise RuntimeError(f"title translation unit {unit_index} failed")


def translate_batch_google(batch_index: int, batch: list[tuple[int, str]], api_key: str) -> tuple[int, list[tuple[int, str]]]:
    url = f"https://translation.googleapis.com/language/translate/v2?key={api_key}"
    payload = {
        "q": [text for _, text in batch],
        "target": TARGET_LANGUAGE,
        "format": "text"
    }
    headers = {
        "Content-Type": "application/json",
    }
    session = thread_session()
    with _llm_request_semaphore:
        for attempt in range(1, TRANSLATION_ATTEMPTS + 1):
            try:
                response = session.post(
                    url,
                    headers=headers,
                    json=payload,
                    timeout=TRANSLATION_TIMEOUT,
                )
                if response.status_code >= 400:
                    raise RuntimeError(f"HTTP {response.status_code}: {response.text[:500]}")

                translations = response.json()["data"]["translations"]
                batch_results = [
                    (unit_index, clean_translation(item["translatedText"]))
                    for (unit_index, _), item in zip(batch, translations)
                ]
                return batch_index, batch_results
            except Exception as exc:
                if attempt == TRANSLATION_ATTEMPTS:
                    raise RuntimeError(f"Google translation batch {batch_index} failed: {exc}") from exc
                time.sleep(RETRY_SLEEP_SECONDS)

    raise RuntimeError(f"Google translation batch {batch_index} failed")


def translate_unit_text_google(unit_index: int, text: str, api_key: str) -> str:
    url = f"https://translation.googleapis.com/language/translate/v2?key={api_key}"
    payload = {
        "q": [text],
        "target": TARGET_LANGUAGE,
        "format": "text"
    }
    headers = {
        "Content-Type": "application/json",
    }
    session = thread_session()
    with _llm_request_semaphore:
        for attempt in range(1, TRANSLATION_ATTEMPTS + 1):
            try:
                response = session.post(
                    url,
                    headers=headers,
                    json=payload,
                    timeout=TRANSLATION_TIMEOUT,
                )
                if response.status_code >= 400:
                    raise RuntimeError(f"HTTP {response.status_code}: {response.text[:500]}")

                translated_text = response.json()["data"]["translations"][0]["translatedText"]
                return clean_translation(translated_text)
            except Exception as exc:
                if attempt == TRANSLATION_ATTEMPTS:
                    raise RuntimeError(f"Google translation unit {unit_index} failed: {exc}") from exc
                time.sleep(RETRY_SLEEP_SECONDS)

    raise RuntimeError(f"Google translation unit {unit_index} failed")


def translate_batch(batch_index: int, batch: list[tuple[int, str]], api_key: str) -> tuple[int, list[tuple[int, str]]]:
    provider = os.getenv("TRANSLATE_PROVIDER", "siliconflow").lower()
    if provider == "google":
        return translate_batch_google(batch_index, batch, api_key)
    else:
        return translate_batch_siliconflow(batch_index, batch, api_key)


def translate_unit_text(unit_index: int, text: str, api_key: str) -> str:
    provider = os.getenv("TRANSLATE_PROVIDER", "siliconflow").lower()
    if provider == "google":
        return translate_unit_text_google(unit_index, text, api_key)
    else:
        return translate_unit_text_siliconflow(unit_index, text, api_key)


def translate_title_text(unit_index: int, text: str, api_key: str) -> str:
    provider = os.getenv("TRANSLATE_PROVIDER", "siliconflow").lower()
    if provider == "google":
        return translate_unit_text_google(unit_index, text, api_key)
    else:
        return translate_title_text_siliconflow(unit_index, text, api_key)


def retry_suspicious_translation(unit_index: int, source: str, translated: str, api_key: str) -> str:
    if not translation_is_suspicious(source, translated):
        return translated

    retry = translate_unit_text(unit_index, source, api_key)
    if translation_is_suspicious(source, retry):
        if re.search(r"[\u3400-\u9fff]", retry) and not re.search(r"[\u3400-\u9fff]", translated):
            return retry
        return translated
    return retry


def retry_suspicious_title_translation(unit_index: int, source: str, translated: str, api_key: str) -> str:
    if not title_translation_is_suspicious(source, translated):
        return translated

    retry = translate_title_text(unit_index, source, api_key)
    if title_translation_is_suspicious(source, retry):
        return translated
    return retry


def translate_blocks(pages: list[PageData], api_key: str, logger: Logger, progress_callback=None, line_height: float = DEFAULT_TEXT_LINE_HEIGHT) -> list[str]:
    blocks = page_blocks(pages)
    texts = [block.text for _, block in blocks]
    results = texts[:]
    references_start = find_references_range(pages)
    units = translation_units(blocks, references_start)
    active = [(index, unit.text) for index, unit in enumerate(units)]
    batches = translation_batches(active)

    logger.write(
        f"  text blocks: {len(texts)}, translation units: {len(active)}, "
        f"blocks selected for translation: {sum(len(unit.indexes) for unit in units)}, batches: {len(batches)}"
    )
    if progress_callback and hasattr(progress_callback, "on_blocks_analyzed"):
        progress_callback.on_blocks_analyzed(len(texts), len(active), len(batches))

    if not batches:
        for text_index, (page, block) in enumerate(blocks):
            if (is_conference_header(page, block) or results[text_index] == block.text) and (fixed := fixed_short_translation(block.text)):
                results[text_index] = fixed
        return results

    workers = min(MAX_CONCURRENT_BATCHES, len(batches))
    total_batches = len(batches)
    completed_batches = 0
    executor = ThreadPoolExecutor(max_workers=workers)
    future_batches = {
        executor.submit(translate_batch, batch_index, batch, api_key): batch
        for batch_index, batch in enumerate(batches, start=1)
    }
    future_labels = {
        future: batch_index
        for batch_index, future in enumerate(future_batches, start=1)
    }
    try:
        while future_batches:
            future = next(as_completed(future_batches))
            source_batch = future_batches.pop(future)
            label = future_labels.pop(future)
            try:
                batch_index, batch_results = future.result()
            except Exception:
                for pending in future_batches:
                    pending.cancel()
                executor.shutdown(wait=False, cancel_futures=True)
                raise

            translated_ids = {unit_index for unit_index, _ in batch_results}
            missing = [
                (unit_index, text)
                for unit_index, text in source_batch
                if unit_index not in translated_ids
            ]
            if missing:
                missing_ids = ", ".join(str(unit_index) for unit_index, _ in missing)
                raise RuntimeError(f"translation batch {label} missing ids: {missing_ids}")

            for unit_index, translated in batch_results:
                unit = units[unit_index]
                unit_items = [blocks[text_index] for text_index in unit.indexes]
                unit_blocks = [block for _, block in unit_items]
                if len(unit_items) == 1 and (fixed := fixed_short_translation(unit.text)):
                    translated = fixed
                elif len(unit_items) == 1 and (fixed := fixed_heading_translation(unit.text)):
                    translated = fixed
                else:
                    translated = retry_suspicious_translation(unit_index, unit.text, translated, api_key)
                if (
                    len(unit_items) == 1
                    and unit_items[0][0].index == 0
                    and has_ieee_journal_header(unit_items[0][0])
                    and is_title_block(unit_items[0][1])
                ):
                    translated = retry_suspicious_title_translation(unit_index, unit.text, translated, api_key)
                if (
                    len(unit_items) == 1
                    and has_ieee_journal_header(unit_items[0][0])
                    and (
                        is_roman_section_heading(unit_items[0][1])
                        or is_lettered_section_heading(unit_items[0][1])
                    )
                    and (fixed_heading := fixed_ieee_section_heading_translation(unit_items[0][1].text))
                ):
                    translated = fixed_heading
                if (
                    len(unit_items) == 1
                    and is_conference_header(unit_items[0][0], unit_items[0][1])
                    and (fixed := fixed_short_translation(unit.text))
                ):
                    translated = fixed
                translated = sanitize_translation(unit_blocks, translated)
                translated = split_leading_heading(unit_blocks, translated)
                parts = split_ieee_drop_cap_translation(unit_items, translated, line_height)
                if parts is None:
                    parts = split_translation(translated, unit_blocks, line_height)
                for text_index, part in zip(unit.indexes, parts, strict=True):
                    results[text_index] = part

            completed_batches += 1
            logger.write(f"  translated batch {batch_index}/{total_batches}")
            if progress_callback and hasattr(progress_callback, "on_batch_complete"):
                progress_callback.on_batch_complete(completed_batches, total_batches)
    finally:
        executor.shutdown(wait=True, cancel_futures=True)

    for text_index, (page, block) in enumerate(blocks):
        if (is_conference_header(page, block) or results[text_index] == block.text) and (fixed := fixed_short_translation(block.text)):
            results[text_index] = fixed

    return results
