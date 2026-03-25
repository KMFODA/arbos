from __future__ import annotations

import sys

from .app import main as _app_main

__all__ = ['main']


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] in {'-h', '--help'}:
        print('Usage: arbos [-p PROJECT] [bot-name|bootstrap-project|migrate-bot-names|send|sendfile|encrypt]')
        return
    _app_main()


if __name__ == '__main__':
    main()
