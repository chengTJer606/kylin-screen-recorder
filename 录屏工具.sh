#!/bin/bash
# 录屏工具 · 启动器（双击运行；若双击询问请选择「运行」）
cd "$(dirname "$0")" || exit 1
exec python3 recorder_gui.py
