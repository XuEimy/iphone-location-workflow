# iPhone Location Workflow

在 Mac 上通过 USB 给自己的 iPhone 设置测试位置，并显式停止和恢复。基于 [pymobiledevice3](https://github.com/doronz88/pymobiledevice3)，无需越狱。

这个仓库记录一次实际完成的操作流程，并提供可复用脚本：**持续模拟时保持异步通信；恢复时发送清除指令后继续保持连接 3 秒；最后在手机地图中验证。** 命令返回成功，不代表地图一定已更新。

## 验证范围

2026-10-29，在一台 iPhone 17e / iOS 27.0.1 和 Mac 上，经操作者确认：

- 持续模拟后，苹果地图到达目标点并保持不变。
- 停止模拟并发送清除指令后，首次观察未恢复；再次清除并保留连接 3 秒，重新打开地图后确认恢复真实位置。
- 再次开启后，操作者确认地图重新到达目标点。

脚本默认使用 `userspace` 连接方式。其他机型、iOS 版本、地图应用，以及拔线或电脑休眠后的效果没有完成实机验证。程序只能报告指令状态；最终位置仍需在手机上确认。

## 准备

- Mac、Python 3.11 或以上、可传输数据的 USB 线。
- iPhone 解锁并信任这台 Mac。
- 在 iPhone 的「设置 → 隐私与安全 → 开发者模式」中开启开发者模式，重启后再次确认。
- 开启地图的位置权限。使用过程中保持 USB 连接和 Mac 唤醒。

开发者模式说明：[Apple 官方文档](https://developer.apple.com/documentation/xcode/enabling-developer-mode-on-a-device)。脚本不会替你修改手机的安全设置。

## 安装

```sh
git clone https://github.com/XuEimy/iphone-location-workflow.git
cd iphone-location-workflow
sh setup.sh
```

依赖固定为 `pymobiledevice3==11.47.0`。只安装在项目的 `.venv` 中，不修改系统 Python。首次安装和下载开发支持组件可能需要几分钟。

## 1. 确认设备并加载开发支持组件

```sh
.venv/bin/pymobiledevice3 usbmux list
```

从列表复制你要操作的设备 UDID。下文中的 `DEVICE_UDID` 都要替换为该值。脚本要求明确指定设备，避免连接多台手机时选错。

```sh
.venv/bin/pymobiledevice3 mounter auto-mount --udid DEVICE_UDID
```

手机重启后可能需要重新执行。该命令可能下载开发支持组件，并向 Apple 请求设备对应的签名；仓库不包含这些组件或配对记录。具体行为见 [上游安装与挂载说明](https://doronz88.github.io/pymobiledevice3/guides/cli-recipes/#developer-mode-and-ddi)。

## 2. 开始持续模拟

替换下面的示例坐标。顺序是 **纬度、经度**，使用 WGS84 十进制度数。不要直接混用高德、百度等地图的坐标值。

```sh
.venv/bin/python location.py start \
  --udid DEVICE_UDID \
  --latitude 48.8584 \
  --longitude 2.2945
```

保持这个终端窗口打开。每 3 秒更新一次相同坐标；不会生成移动路线。按 `Ctrl+C` 会停止更新，并尝试清除模拟位置。

在苹果地图中点击「我的位置」，放大到道路或建筑，再观察一段时间。不要用地图视窗中心或搜索结果代替当前位置蓝点。如果地图不对，先停止，确认连接和坐标，再重试。

默认 `--transport userspace`。仅在排查连接问题时尝试 `--transport native`；macOS 原生通道可能与 Apple 的后台服务争用连接。见 [上游连接说明](https://doronz88.github.io/pymobiledevice3/guides/ios17-tunnels/)。

## 3. 停止并恢复

在另一个终端进入同一仓库目录，执行：

```sh
.venv/bin/python location.py stop
```

它会通知当前进程停止更新、发送清除指令、保留连接 3 秒，再退出。脚本不会杀死其他进程。如果没有正在运行的会话，它会明确报告，而不会声称位置已恢复。

如果原进程已退出、手机曾断线，或者地图仍停在模拟位置，保持手机连接并解锁，显式清除：

```sh
.venv/bin/python location.py clear --udid DEVICE_UDID
```

**重新打开地图，点击「我的位置」，等待约 15 秒，确认真实位置。** 如果仍未恢复，停止其他定位工具后重试。必要时手动重启 iPhone，再用地图核对。不要仅凭 `Clear command sent` 判断恢复成功。

## 状态与限制

```sh
.venv/bin/python location.py status
```

`running` 来自进程持有的文件锁；`last_report` 是最近一次指令状态，可能已经过时，不能证明当前 GPS 坐标。一个用户同时只允许一个本工具会话，以免恢复指令与另一会话的更新互相覆盖。

运行状态写入 Mac 的 `~/Library/Caches/iphone-location-workflow/`，不记录 UDID、坐标或位置历史。命令行参数本身会出现在终端历史及本机进程列表中。不要把带设备编号的终端记录、配对文件或截图提交到仓库。

目前没有图形界面、开机启动、自动重连或后台常驻服务。USB 断开、强制退出或电脑休眠时，清除操作可能无法到达手机；请重新连接后运行 `clear` 并人工验证。脚本使用 macOS/POSIX 文件锁和信号，尚未提供 Windows 支持。

## 为什么保持异步通信

在固定的上游版本中，`dvt simulate-location set` 在发送坐标后调用同步 `wait_return()`；macOS 实现使用 `signal.sigwait()`。本次观察到「命令成功，但地图未变或没有持续保持」的现象。阻塞等待可能影响同一事件循环中的连接任务，但本次没有完成严格的因果隔离实验。

本工具只在自身进程中替换该命令的等待路径：使用异步循环持续更新，并在退出时显式清除。`clear()` 是不等待设备回复的消息，所以清除后保留事件循环 3 秒作为传输窗口。该等待不是设备定位状态的确认。未修改已安装的上游文件。

升级依赖后应重新核对 API、运行测试并进行实机验证，不能直接移除版本限制。

## 测试

无需连接手机即可运行生命周期测试：

```sh
python3 -m unittest discover -s tests -v
```

测试覆盖停止、取消、更新失败后的清除尝试、清除失败、传输等待、重复启动以及无效坐标。GitHub Actions 运行这些测试，不执行手机定位，也不等同于设备兼容性验证。

完整操作及发布验收见 [工作流说明](docs/workflow.md)。

## 许可证与致谢

[GPL-3.0-or-later](LICENSE)。此项目调用并适配 pymobiledevice3 的接口；设备协议与连接能力来自 [doronz88/pymobiledevice3](https://github.com/doronz88/pymobiledevice3)。不随仓库分发 Apple 开发镜像、配对材料或其他设备文件。
