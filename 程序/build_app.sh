#!/bin/zsh
# 生成「玉衡5.0.app」：标准 Mac 程序（M 芯片原生运行），本机签名，图标用玉衡标志
# 用法：zsh 程序/build_app.sh   （在项目文件夹里运行；桌面上的文件夹名还是「赚钱小助手」）
set -e
cd "$(dirname "$0")/.."
APP="玉衡5.0.app"
rm -rf "$APP" "玉衡5.0.app"  # 顺手删掉旧名字的 App
osacompile -o "$APP" "程序/app_launcher.applescript"
cp "程序/app_start.sh" "$APP/Contents/Resources/start.sh"
chmod +x "$APP/Contents/Resources/start.sh"
cp "程序/brand/yuheng.icns" "$APP/Contents/Resources/applet.icns"
P="$APP/Contents/Info.plist"
plutil -replace CFBundleIdentifier -string "local.zhuanqian.app" "$P"
plutil -replace CFBundleName -string "玉衡5.0" "$P"
plutil -replace CFBundleDisplayName -string "玉衡5.0" "$P" 2>/dev/null || plutil -insert CFBundleDisplayName -string "玉衡5.0" "$P"
plutil -replace NSMicrophoneUsageDescription -string "语音问答：拍两下 Mac 后听你的问题" "$P"
codesign --force --deep --sign - "$APP" 2>/dev/null || true
echo "✓ 已生成 $APP"
