````markdown
# 局域网大文件断点续传工具（Windows GUI + 全局热键）技术方案

> 目标：在 Windows 上做一个带 GUI 的局域网文件传输工具，支持大文件、断点续传、全局热键唤起窗口。远程端为 Windows（或 Linux）。

---

## 1. 需求与关键技术决策

| 需求 | 结论 |
|---|---|
| 局域网传输 | SSH / SFTP 直连即可，无需 NAT 穿透 |
| 大文件 | 必须支持断点续传；注意 SCP 的 4GB 32 位边界问题 |
| 断点续传 | **SCP 协议本身不支持续传**，必须改用 **SFTP** |
| GUI | PySide6（Qt for Python） |
| 全局热键 | Win32 `RegisterHotKey` + `QAbstractNativeEventFilter` |
| 平台 | Windows（远程端 Windows / Linux 均可） |
| 分发 | PyInstaller 打包为单文件 exe |

### ⚠️ 最重要的一条

**放弃 SCP，改用 SFTP。**

- SCP 协议没有续传能力（WinSCP 官方文档也明确：SCP 和 WebDAV 不允许续传）。
- SCP 对 >4GB 文件存在 32 位尺寸边界问题。
- SFTP 使用 64 位文件尺寸，原生适合大文件。
- 底层仍是 SSH，端口 22，认证方式不变。

**续传逻辑要自己实现**：比对远程与本地文件大小 → 本地 `seek` 到断点 → 只上传剩余部分。

---

## 2. 整体架构

```
全局热键 (Win32 RegisterHotKey)
        │ 按下热键
        ▼
  主窗口显示/唤起 (PySide6 QMainWindow)
        │ 用户选择文件 & 点击发送
        ▼
  后台传输线程 (QThread) ─── SFTP 断点续传 (Paramiko) ─── 远程主机
        │ 进度信号 (Signal)
        ▼
  GUI 进度条更新 (主线程)
```

**核心原则**：所有网络传输放在 `QThread`，主线程只负责 UI。进度通过 `Signal` 回传，避免界面卡死。

---

## 3. 依赖

```bash
pip install PySide6 paramiko pyinstaller
```

| 库 | 用途 |
|---|---|
| PySide6 | GUI、QThread、原生事件过滤器 |
| paramiko | SSH / SFTP 客户端 |
| pyinstaller | 打包 exe |

---

## 4. 核心模块一：SFTP 断点续传

```python
import os
import paramiko


def sftp_resume_upload(host, port, username, password,
                       local_path, remote_path, progress_cb=None):
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(host, port=port, username=username, password=password)
    sftp = ssh.open_sftp()

    try:
        # 1. 读取远程已存在文件的大小（不存在视为 0）
        try:
            remote_size = sftp.stat(remote_path).st_size
        except FileNotFoundError:
            remote_size = 0

        local_size = os.path.getsize(local_path)

        if remote_size >= local_size:
            return  # 已传完

        # 2. 本地 seek 到断点，只上传剩余部分
        with open(local_path, "rb") as f:
            f.seek(remote_size)

            def _callback(sent, total):
                if progress_cb:
                    progress_cb(remote_size + sent, local_size)

            sftp.putfo(
                f,
                remote_path,
                file_size=local_size - remote_size,  # 关键：剩余字节数
                callback=_callback,
            )
    finally:
        sftp.close()
        ssh.close()
```

**关键点**

- `putfo` 的 `file_size` 必须是 **剩余字节数**（`local_size - remote_size`），不是文件总大小。传错会导致远端文件被截断或写入异常。
- 生产环境可加：校验远程尾部一段哈希，防止远程部分文件被篡改后仍续传。
- Windows 远程端路径示例：`/C:/Users/Public/Desktop/xxx.zip`（SFTP 使用正斜杠）。

---

## 5. 核心模块二：全局热键

Qt 的 `QShortcut` 只在窗口有焦点时生效，无法实现"全局唤起"。Windows 上需调用 `user32.dll` 的 `RegisterHotKey`，并用 `QAbstractNativeEventFilter` 拦截 `WM_HOTKEY` 消息。

```python
import ctypes
from ctypes import wintypes
from PySide6.QtCore import QAbstractNativeEventFilter, QObject, Signal

MOD_ALT      = 0x0001
MOD_CONTROL  = 0x0002
MOD_SHIFT    = 0x0004
MOD_WIN      = 0x0008
MOD_NOREPEAT = 0x4000
WM_HOTKEY    = 0x0312


class GlobalHotkeyFilter(QAbstractNativeEventFilter):
    triggered = Signal()

    def __init__(self, hotkey_id=1):
        super().__init__()
        self.hotkey_id = hotkey_id

    def nativeEventFilter(self, eventType, message):
        if eventType == b"windows_generic_MSG":
            msg = ctypes.cast(
                int(message), ctypes.POINTER(wintypes.MSG)
            ).contents
            if msg.message == WM_HOTKEY and msg.wParam == self.hotkey_id:
                self.triggered.emit()
                return True, 0
        return False, 0


class HotkeyManager(QObject):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.hotkey_id = 1
        self._filter = GlobalHotkeyFilter(self.hotkey_id)

    def register(self, vk_code,
                 modifiers=MOD_CONTROL | MOD_ALT | MOD_NOREPEAT):
        if not ctypes.windll.user32.RegisterHotKey(
                None, self.hotkey_id, modifiers, vk_code):
            raise OSError("热键注册失败，可能已被占用")

    def unregister(self):
        ctypes.windll.user32.UnregisterHotKey(None, self.hotkey_id)

    def install(self, app):
        app.installNativeEventFilter(self._filter)
```

**使用（Ctrl + Alt + Space 唤起，`VK_SPACE = 0x20`）**

```python
hotkey = HotkeyManager()
hotkey.register(vk_code=0x20,
                modifiers=MOD_CONTROL | MOD_ALT | MOD_NOREPEAT)
hotkey.install(app)
hotkey._filter.triggered.connect(
    lambda: (window.showNormal(), window.activateWindow())
)
```

**注意**：`RegisterHotKey` 需要在注册热键的线程中处理消息循环。上述方案利用 Qt 主线程的消息泵，比另开线程更稳。

---

## 6. 核心模块三：GUI + 传输线程

```python
from PySide6.QtCore import QThread, Signal


class TransferThread(QThread):
    progress = Signal(int, int)    # 已传字节, 总字节
    finished = Signal(bool, str)   # 成功?, 消息

    def __init__(self, ssh_config, local_path, remote_path):
        super().__init__()
        self.ssh_config = ssh_config
        self.local_path = local_path
        self.remote_path = remote_path

    def run(self):
        try:
            sftp_resume_upload(
                **self.ssh_config,
                local_path=self.local_path,
                remote_path=self.remote_path,
                progress_cb=lambda sent, total:
                    self.progress.emit(sent, total),
            )
            self.finished.emit(True, "传输完成")
        except Exception as e:
            self.finished.emit(False, str(e))
```

主窗口连接方式：

```python
self.thread = TransferThread(ssh_config, local, remote)
self.thread.progress.connect(self.on_progress)   # 更新 QProgressBar
self.thread.finished.connect(self.on_finished)
self.thread.start()
```

`on_progress` 中只做 UI 更新（设置进度条 range / value、格式化文本），**不要**做任何阻塞操作。

---

## 7. 完整可运行骨架

```python
import sys, ctypes, os
from ctypes import wintypes
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout,
    QPushButton, QLineEdit, QProgressBar, QLabel, QFileDialog
)
from PySide6.QtCore import QThread, Signal, QAbstractNativeEventFilter, QObject

# ---------- SFTP 续传 / 全局热键（见第 4、5 节，略） ----------


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("局域网快传")
        self.resize(520, 280)

        central = QWidget()
        layout = QVBoxLayout(central)

        self.host_edit = QLineEdit(placeholderText="目标 IP")
        self.user_edit = QLineEdit(placeholderText="用户名")
        self.pass_edit = QLineEdit(placeholderText="密码")
        self.pass_edit.setEchoMode(QLineEdit.Password)

        self.file_btn   = QPushButton("选择文件")
        self.file_label = QLabel("未选择")
        self.send_btn   = QPushButton("发送")
        self.progress   = QProgressBar()
        self.status     = QLabel("就绪")

        for w in (self.host_edit, self.user_edit, self.pass_edit,
                  self.file_btn, self.file_label, self.send_btn,
                  self.progress, self.status):
            layout.addWidget(w)

        self.setCentralWidget(central)

        self.local_path = None
        self.thread = None
        self.file_btn.clicked.connect(self.pick_file)
        self.send_btn.clicked.connect(self.start_send)

    def pick_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择要发送的文件")
        if path:
            self.local_path = path
            self.file_label.setText(os.path.basename(path))

    def start_send(self):
        if not self.local_path:
            self.status.setText("请先选择文件")
            return
        if self.thread and self.thread.isRunning():
            return

        ssh_config = {
            "host":     self.host_edit.text().strip(),
            "port":     22,
            "username": self.user_edit.text().strip(),
            "password": self.pass_edit.text(),
        }
        # Windows 远程端示例路径
        remote_path = (
            f"/C:/Users/Public/Desktop/{os.path.basename(self.local_path)}"
        )

        self.thread = TransferThread(ssh_config, self.local_path, remote_path)
        self.thread.progress.connect(self.on_progress)
        self.thread.finished.connect(self.on_finished)
        self.thread.start()
        self.send_btn.setEnabled(False)

    def on_progress(self, sent, total):
        self.progress.setMaximum(total)
        self.progress.setValue(sent)
        self.status.setText(
            f"{sent / 1024 / 1024:.1f} / {total / 1024 / 1024:.1f} MB"
        )

    def on_finished(self, ok, msg):
        self.send_btn.setEnabled(True)
        self.status.setText(msg)
        if ok:
            self.progress.setValue(self.progress.maximum())


def main():
    app = QApplication(sys.argv)
    window = MainWindow()

    hotkey = HotkeyManager()
    hotkey.register(vk_code=0x20,
                    modifiers=MOD_CONTROL | MOD_ALT | MOD_NOREPEAT)
    hotkey.install(app)
    hotkey._filter.triggered.connect(
        lambda: (window.showNormal(), window.activateWindow())
    )

    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
```

---

## 8. 打包与发布

```bash
pyinstaller --onefile --windowed --name "LANFileDrop" main.py
```

- `--windowed`：不弹控制台窗口。
- 全局热键需要程序处于运行状态，建议做**开机自启**：把 exe 快捷方式放到 `shell:startup` 目录。

---

## 9. 远程端准备（Windows）

1. 安装并启动 **OpenSSH Server**（Windows 可选功能里可一键安装）。
2. 允许 22 端口入站（防火墙放行）。
3. 确认登录用户有目标目录写权限。
4. 目标路径使用 SFTP 格式（正斜杠 + 盘符前缀），如：
   - `/C:/Users/Public/Desktop/`
   - `/D:/transfer/`

---

## 10. 安全与可靠性清单

- [ ] 使用 SFTP，不用 SCP（续传 + 大文件支持）
- [ ] 密码存储避免明文：改用 SSH Key 或 Windows 凭据管理器
- [ ] 远程路径仅允许写入指定目录，防止路径穿越
- [ ] 续传前可校验远程尾部哈希，避免部分文件被篡改
- [ ] 网络断线自动重试（装饰器 / 循环 + 退避）
- [ ] 显示实时进度、速度、剩余时间
- [ ] 支持多文件/文件夹（递归上传）
- [ ] 日志落盘（排查续传失败原因）
- [ ] 局域网环境可考虑 SSH 密钥免密，避免每次输密码

---

## 11. 备选引擎：trzsz

如果不想自己维护续传逻辑，可参考现成命令行工具 **trzsz**：

- 支持断点续传（`trz -y` / `tsz -y` 覆盖同名文件时自动续传）
- 可在 Windows 客户端和 Windows SSH 服务器之间运行
- 可以将其作为传输引擎嵌入，或借鉴其续传判断策略

---

## 12. 方案总结

| 层 | 选型 |
|---|---|
| 协议 | **SFTP**（不是 SCP） |
| 传输库 | Paramiko |
| 续传策略 | 比对文件大小 + seek + putfo(file_size=剩余字节) |
| GUI | PySide6 |
| 并发 | QThread + Signal |
| 全局热键 | Win32 `RegisterHotKey` + `QAbstractNativeEventFilter` |
| 分发 | PyInstaller 单文件 exe + 开机自启 |

**一句话结论**：放弃 SCP，用 SFTP + Paramiko 做续传；GUI 用 PySide6；热键用 Win32 原生 API；传输放 QThread，进度用 Signal 回传。
````