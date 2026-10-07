#!/bin/bash
echo "🔧 应用补丁..."
python3 /app/patches/apply_all_patches.py
echo "🚀 启动应用..."
exec "$@"
