# Fluent 界面（已接入真实设备）

新 Fluent 面板与电量环已接入真实采集和后台控制，设备协议、配置格式、自启/桌面链接的显式语义没有改动。

## 打开

```powershell
D:\Python312\pythonw.exe my_devices.py                    # 真正读取设备的 Fluent 面板
D:\Python312\pythonw.exe ui_preview.py --material mica    # 纯演示数据，不读硬件
双击 预览新界面.bat                                        # 同上（演示）
```

面板中的“设置与后台”可改刷新间隔、开关自动刷新、显式设置开机自启、重建桌面链接、显示电量环或退出后台。“显示电量环”等同于再次启动后台，不会把运行中的后台关掉。“设备详情”展示当前真实读数与来源说明。

## 设计与真实透明的边界

- 面板顶部保留 84px 的 Windows 系统材质（Acrylic / Mica），其下由一条逐像素透明渐变条平滑过渡到不透明深色内容区：交界处无硬边，且所有文字/按钮都落在不透明区域，亮色背景不会让标题发灰。
- 电量环在**任务栏内部**渲染（实测窗口矩形完全包含于任务栏矩形），使用系统字体抗锯齿数字与圆头细弧。空轨道颜色随采样到的任务栏底色自适应，浅色任务栏不再出现沉重黑圈；低于 25% / 10% 才转琥珀/红色并保持告警色可读。
- 手柄仍只用四段档位，有线用插头标记，不伪造百分比。
- 使用文档化 `DwmSetWindowAttribute`(20/33/38) 与 `DwmExtendFrameIntoClientArea`；低版本、高对比、系统透明关闭、API 失败会自动回退深色实底；渐变条创建失败同样回退实底，不会出现半套效果。
- 不使用整窗透明度、颜色穿透键或动画轮询。渐变条是 `WS_EX_NOACTIVATE | WS_EX_TRANSPARENT` 的点击穿透、非置顶子窗口，随主窗口隐藏/关闭清理。
- 前台全屏临时隐藏、会话内手动隐藏、显式恢复的语义与原版一致。

## 验证

```powershell
D:\Python312\python.exe -B -m unittest discover -s tests -q
D:\Python312\python.exe -B tests\windows_restart_smoke.py --run
D:\Python312\python.exe -B tests\windows_ui_preview_smoke.py --run --out D:\ai\projects\battery-investigation\fluent-ui-final
D:\Python312\python.exe -B tests\windows_backdrop_probe.py --run --selective --summary
```

- 269 项离线测试通过（不触硬件/桌面）。
- 原生验收覆盖四场景、三种材质、最小尺寸、详情、浮窗隐藏后刷新不复活、逐像素透明、稳态 GDI/USER 无增长。
- 真实后台重启/恢复与全屏隐藏仍通过隔离跨进程测试。
- 尚未对新的面板做真实独占 D3D/反作弊游戏、多屏、运行中 DPI 切换的完整验收。
- 截图/探测脚本使用环境中已有的 Pillow；应用运行时仍是标准库 + Tk + 系统 DLL。
