#!/usr/bin/env bash
set -euo pipefail

# ==============================================================================
# 研申 (YanShen) - macOS 原生 Application 打包脚本
# 支持 Apple Silicon (M1/M2/M3/M4) 与 Intel (x86_64) 架构
# ==============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${ROOT_DIR}"

echo "============================================================"
echo " [研申] 开始构建 macOS 原生应用 (研申.app)..."
echo " 工作目录: ${ROOT_DIR}"
echo " 系统架构: $(uname -m)"
echo "============================================================"

# 1. 检查 Python 虚拟环境
PYTHON_BIN="${ROOT_DIR}/.venv/bin/python"
if [ ! -f "${PYTHON_BIN}" ]; then
  if command -v python3 &>/dev/null; then
    PYTHON_BIN="python3"
  else
    echo "❌ 错误: 未找到 Python 执行环境，请先初始化 .venv"
    exit 1
  fi
fi

echo "--> 使用 Python: $("${PYTHON_BIN}" --version) (${PYTHON_BIN})"

# 2. 检查关键打包依赖
echo "--> 检查依赖项..."
"${PYTHON_BIN}" -c "import webview; import objc; import PyInstaller" 2>/dev/null || {
  echo "⚠️ 正在安装缺失的打包依赖 (pyinstaller, pywebview, pyobjc)..."
  "${PYTHON_BIN}" -m pip install pyinstaller pywebview pyobjc-framework-WebKit pyobjc-framework-Cocoa
}

# 3. 校验题库与知识库完整性
echo "--> 校验题库种子与名师知识库..."
"${PYTHON_BIN}" scripts/audit_release.py

# 4. 确保 macOS 图标 (.icns) 存在
if [ ! -f "assets/app-icon.icns" ] && [ -f "assets/app-icon.png" ]; then
  echo "--> 生成 assets/app-icon.icns..."
  ICONSET_DIR="/tmp/yanshen_app.iconset"
  mkdir -p "${ICONSET_DIR}"
  sips -z 16 16     assets/app-icon.png --out "${ICONSET_DIR}/icon_16x16.png" >/dev/null 2>&1
  sips -z 32 32     assets/app-icon.png --out "${ICONSET_DIR}/icon_16x16@2x.png" >/dev/null 2>&1
  sips -z 32 32     assets/app-icon.png --out "${ICONSET_DIR}/icon_32x32.png" >/dev/null 2>&1
  sips -z 64 64     assets/app-icon.png --out "${ICONSET_DIR}/icon_32x32@2x.png" >/dev/null 2>&1
  sips -z 128 128   assets/app-icon.png --out "${ICONSET_DIR}/icon_128x128.png" >/dev/null 2>&1
  sips -z 256 256   assets/app-icon.png --out "${ICONSET_DIR}/icon_128x128@2x.png" >/dev/null 2>&1
  sips -z 256 256   assets/app-icon.png --out "${ICONSET_DIR}/icon_256x256.png" >/dev/null 2>&1
  sips -z 512 512   assets/app-icon.png --out "${ICONSET_DIR}/icon_256x256@2x.png" >/dev/null 2>&1
  sips -z 512 512   assets/app-icon.png --out "${ICONSET_DIR}/icon_512x512.png" >/dev/null 2>&1
  sips -z 1024 1024 assets/app-icon.png --out "${ICONSET_DIR}/icon_512x512@2x.png" >/dev/null 2>&1
  iconutil -c icns "${ICONSET_DIR}" -o assets/app-icon.icns
  rm -rf "${ICONSET_DIR}"
fi

# 5. 执行 PyInstaller 打包
echo "--> 执行 PyInstaller 构建..."
"${PYTHON_BIN}" -m PyInstaller --clean -y 研申_mac.spec

# 6. 验证产物结构
APP_PATH="dist/研申.app"
if [ ! -d "${APP_PATH}" ]; then
  echo "❌ 错误: 未生成 ${APP_PATH}"
  exit 1
fi

echo "--> 进行本地 ad-hoc 签名 (避免 macOS 安全隔离拦截)..."
codesign --force --deep -s - "${APP_PATH}" || echo "⚠️ codesign 签名提示（可忽略）"

APP_SIZE=$(du -sh "${APP_PATH}" | cut -f1)

echo "============================================================"
echo "🎉 macOS 原生应用打包成功!"
echo "   产物路径: ${ROOT_DIR}/${APP_PATH}"
echo "   包体大小: ${APP_SIZE}"
echo "   架构类型: $(file "${APP_PATH}/Contents/MacOS/研申" | awk -F: '{print $2}')"
echo ""
echo "🚀 运行方式:"
echo "   - 双击直接启动: 打开 Finder -> 进入 dist 目录 -> 双击 '研申.app'"
echo "   - 终端命令启动: open \"${APP_PATH}\""
echo "============================================================"
