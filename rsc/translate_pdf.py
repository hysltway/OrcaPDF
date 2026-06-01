from __future__ import annotations

import sys

try:
    from rsc.pdf_translate import *
except ModuleNotFoundError as exc:
    if exc.name != "rsc":
        raise
    from pdf_translate import *


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
