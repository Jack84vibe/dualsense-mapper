# -*- coding: utf-8 -*-
"""JS 现拼出来的字符串。

这些不在渲染好的 DOM 里整块出现（带插值、或者由状态决定），所以
build_i18n.py 那个「跑起来再抓」的办法收不到它们 —— 它们是在
index.html 里由 t() 包住、运行时才拼起来的。手写在这里。
"""
EN = {
# 连接状态栏
"蓝牙":"Bluetooth","USB":"USB"," 已连接":" connected"," · 电量 ":" · battery ",
" 充电中":" charging","未检测到手柄":"No controller detected",
"映射已暂停":"Mapping paused","映射已启用":"Mapping active",
"映射已恢复":"Mapping resumed","紧急暂停 ":"Emergency pause ",
# 陀螺仪实时读数
"已从手柄读到":"read from the controller",
"读不到，按名义刻度估算":"not available — estimating from the nominal scale",
"静止":"still","正在转动":"rotating","（倾斜 ":" (tilting ",
" 度/秒）":" deg/s)"," 度/秒":" deg/s",
# 组合键录制
"组合键已设为 ":"Combo set to ",
"在手柄上同时按住想用的组合键，松开即保存":
  "Hold the buttons you want together on the controller; release to save",
"至少要两个键同按，免得误触。再试一次":
  "At least two buttons at once, so it can't be hit by accident. Try again",
"请在手柄上同时按住 2~3 个键（扳机要按到底）…":
  "Hold 2–3 buttons together on the controller (triggers must be pressed fully)…",
"未设置":"Not set",
# 各种提示
"零点已重新校准":"Zero recalibrated","已重命名":"Renamed",
"已复制一份，并切到新的这一档":"Copied, and switched to the new profile",
"已恢复出厂映射":"Factory mapping restored","已切换配置档":"Profile switched",
"陀螺仪已开启":"Gyro on","陀螺仪已关闭":"Gyro off",
"手柄已连接（":"Controller connected (","）":")",
"录音设备：":"Recording device: ",
"已切换默认录音设备":"Default recording device switched",
"切换失败，请在系统声音设置里手动改":
  "Couldn't switch — please change it manually in Windows sound settings",
}
