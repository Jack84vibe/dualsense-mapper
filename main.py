"""程序入口。双击 exe 或 python main.py 都从这里启动。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ps5mapper.app import main  # noqa: E402

if __name__ == "__main__":
    main()
