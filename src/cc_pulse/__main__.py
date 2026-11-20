"""支持 `python -m cc_pulse` 直接运行。"""

import sys

from cc_pulse.cli import main

if __name__ == "__main__":
    sys.exit(main())
