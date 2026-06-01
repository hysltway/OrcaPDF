from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path

@dataclass(slots=True)
class TextBlock:
    rect: tuple[float, float, float, float]
    text: str
    font_size: float
    line_count: int
    bold: bool
    italic: bool
    leading_bold: bool
    align: str


@dataclass(slots=True)
class PageData:
    index: int
    width: float
    height: float
    blocks: list[TextBlock]


class Logger:
    def __init__(self, path: Path, progress_callback=None) -> None:
        self.file = path.open("w", encoding="utf-8")
        self.progress_callback = progress_callback
        self.lock = threading.Lock()

    def write(self, message: str = "") -> None:
        with self.lock:
            self.file.write(message + "\n")
            self.file.flush()
            try:
                print(message)
            except UnicodeEncodeError:
                print(message.encode("ascii", errors="replace").decode("ascii"))
            if self.progress_callback and hasattr(self.progress_callback, "on_log"):
                self.progress_callback.on_log(message)

    def close(self) -> None:
        with self.lock:
            self.file.close()


@dataclass(slots=True)
class Unit:
    indexes: list[int]
    text: str


@dataclass(slots=True)
class LayoutGroup:
    blocks: list[TextBlock]
    text: str
    hidden: bool = False

