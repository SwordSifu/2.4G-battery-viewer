# 我的三设备电量（SwordSifu fork）

这是 [2.4G-battery-viewer](https://github.com/SwordSifu/2.4G-battery-viewer) 的个人定制分支，
只适配**本机的三台设备**，不做通用设备探测。

| 设备 | 身份 | 读数方式 | 精度 |
|---|---|---|---|
| VXE R1 NK 鼠标（星闪） | `373B:10C9`，FF02:2，17/17 字节 | COMpx 接收器查询 03/04 | **真实百分比** |
| 洛斐键盘 | `05AC:024F`，FF02:2，17/17 字节 | COMpx 接收器查询 03/04（byte5=0x80） | **真实百分比** |
| 八位堂 Ultimate 3mode Xbox | `2DC8:2030` | XInput 电池档位 API | **仅四档，无百分比** |

> 手柄这块必须说清楚：XInput 只给 EMPTY/LOW/MEDIUM/FULL 四档。
> Windows 的 `BatteryReport` 还会把档位映射成 700/1000 这类合成容量，
> 那是固定档位映射、不是实测值，所以界面只显示「中档」，绝不写成 70%。

## 用法

```bash
python my_devices.py             # 打开面板（默认 60 秒自动刷新）
python my_devices.py --once      # 命令行读一次
python my_devices.py --autostart status
python tray_app.py               # 托盘常驻（pythonw 静默运行）
python tray_app.py --status
python tray_app.py --quit
```

开机自启与桌面链接都是**显式操作**：只有你在界面点按钮或运行
`--autostart on` / `--install-links` 才会创建；常驻程序只读真实状态，
不会自动纠正或删除任何启动项。

## 已验证的协议（2026-10-21 本机实测）

请求帧 17 字节，首字节 `08` 为 Report ID，末字节校验：

```text
校验 = (0x55 - sum(前16字节)) & 0xFF
鼠标电量  08 04 00 00 00 00 00 00 ... 49
键盘电量  08 04 00 00 00 80 00 00 ... C9
在线查询  08 03 ...                    （鼠标 byte5=00 / 键盘 byte5=80）
```

回复要求：长度 17、`response[0:5]` 与请求前 5 字节一致、全帧加和为 `0x55`；
`response[5] & 0x7F` 是有效数据长度；电量取 `response[6]`，大于 100 直接判错
（不钳位，避免把错设备的回复显示成合理百分比）。

实测回复（真实硬件）：

```text
鼠标  08 04 00 00 00 02 5a 00 0f b3 00 00 00 00 00 00 2b  -> 90%
键盘  08 04 00 00 00 84 64 00 10 bc 00 00 00 00 00 00 95  -> 100%
```

## 实现要点

- **零第三方依赖**：只用 Python 标准库 + ctypes + tkinter。
- **同一接收器不并发读**：按 HID 路径用命名互斥量串行化，避免两个进程抢同一句柄。
- **不炸进程**：读和写用各自独立的 `FILE_FLAG_OVERLAPPED` 句柄；
  超时只用 `CancelIoEx` 发取消请求，**等在途 I/O 真正完成**后才释放缓冲区和事件，
  绝不在 IO 未完成时释放内存。
- **不写日志**：默认没有任何文件输出；只有 `--debug` 才打印到控制台。
- **界面**：后台线程只把结果丢进 `queue.Queue`，主线程用 `after` 取，Tk 不跨线程调用。
- **托盘**：`SetTimer` 驱动刷新，空闲不轮询；图标用点阵数字渲染，内存生成 HICON。
- **退出干净**：`WM_DESTROY` 里 `PostQuitMessage`；先停定时器、移除图标、再销毁窗口，
  最后才释放单实例互斥量。实测连续启停 3 次无残留进程与图标。
- **独立命名空间**：窗口类、互斥量、消息名、启动项、桌面链接都与上游版本不同，
  两个版本可以共存，不会互相发退出消息或抢窗口。

## 验收记录（本机实测）

| 项目 | 结果 |
|---|---|
| 鼠标读数 | 90%，放电中 |
| 键盘读数 | 100%，充电指示关闭 |
| 手柄读数 | 中档（碱性电池），明确标注非百分比 |
| 连续启停 3 次 | 通过，无残留进程/图标 |
| 空闲 75 秒 CPU | 0.031 秒（要求 < 0.5 秒） |
| 离线单元测试 | 27 项通过（不触硬件） |

## 许可证

本仓库分两部分，授权范围不同，请勿混为一谈：

- **SwordSifu 原创部分**（`personal_hid.py`、`personal_devices.py`、测试、
  以及对面板/托盘/图标的改写）采用 [MIT](LICENSE)。
- **来自上游的部分**（`app.py`、`battery.py`、`bt_battery.py`、`darkrim_proto.py`、
  `hid_win.py`、`win_link.py`、`win_shell.py`、`PROMPT.md` 等）来自
  [yszyszysz233/2.4G-battery-viewer](https://github.com/yszyszysz233/2.4G-battery-viewer)，
  上游**没有附带开源许可证**，README 注明“仅供学习与个人使用”。
  因此这部分不随本仓库的 MIT 授权，整体再分发或商用请另行取得上游授权。

详细划分见 [NOTICE](NOTICE)。

## 说明

私有协议来自对本机已安装厂商软件的静态分析加真机只读验证，
仅供个人学习与自用。
