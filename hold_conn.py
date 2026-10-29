#!/usr/bin/env python3
"""占住一个 keep-alive 连接，用来复现「有连接时 stop 挂死」。"""
import socket
import sys
import time

s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.connect("/vol1/@appcenter/erm/erm.sock")
s.sendall(b"GET /app/erm/api?action=status HTTP/1.1\r\nHost: localhost\r\n\r\n")
s.recv(65536)                      # 读一次响应，然后故意不关连接
print("holding keep-alive connection", flush=True)
time.sleep(int(sys.argv[1]) if len(sys.argv) > 1 else 40)
s.close()
print("released", flush=True)
