"""兼容原启动命令；文件音频统一进入录音队列，避免循环累计与重复入库。"""
from worker.recordings_worker import main

if __name__ == '__main__':
    main()
