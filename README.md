# LANDrop

Windows 局域网文件传输工具，采用 **Vue 3 + Tauri 2 桌面壳 + Python 本地传输服务**。桌面应用启动时会自动拉起 Python sidecar，通过带会话令牌的 loopback API 驱动 Paramiko 完成 SSH/SFTP 单文件传输，支持断点续传与 SHA-256 校验。

## 当前进度

已完成计划书阶段一至阶段八：

- 三部分工程骨架、Tauri 2 桌面壳、最小 capabilities。
- Python 服务由 Tauri 自动启动：随机端口、32 字节高熵令牌、`/health` 就绪探测、退出清理。
- 本机 API 仅监听 `127.0.0.1`，校验令牌与请求来源，任务化传输、SSE 进度、取消、重试、错误映射。
- Vue 界面已接入真实 API：连接配置、认证方式、主机指纹确认、文件选择、发送/取消/重试、进度与速度。
- 全局快捷键从配置读取（默认 `Ctrl+Alt+Space`），启动即注册，界面内可修改；被占用时降级为普通窗口操作。
- 真实 SFTP 端到端测试：新传、断点续传、主机密钥拒绝与变更、覆盖保护、认证失败。
- 已产出 Windows NSIS 安装包，sidecar 为 PyInstaller onedir；安装/启动/卸载全流程已实测。

尚未包含：多文件队列与设备发现、代码签名、自动更新。

## 安装包

打包产物是一个 NSIS 安装包，装完的机器不需要 Node 或 Python：

```powershell
npm install
python -m pip install -e ".[dev]"
npm run package
```

产物：`src-tauri\target\release\bundle\nsis\LANDrop_0.2.0_x64-setup.exe`（约 19 MB）。

打包分两步：PyInstaller 把 Python 服务打成 onedir 目录，再让 Tauri 收进安装包。首次打包 Tauri 需要从 GitHub 下载 NSIS，网络受限时见[技术实现](docs/技术实现.md)第 12.4 节。

## 前置环境

安装 Node.js、Rust stable、Python 3.11 或更高版本，以及 Tauri 所需的 Windows WebView2 和 C++ 构建工具。Rustup 默认安装目录如不在当前终端 PATH，需要先将 `%USERPROFILE%\.cargo\bin` 加入 PATH。

Python 依赖：

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
```

## 启动桌面应用

```powershell
npm install
npm run tauri dev
```

桌面应用会自动启动 Python 服务。开发模式下 Rust 侧调用 `python -m landrop.api.app`（需已安装 Python 依赖），并通过 `PYTHONPATH` 指向 `src/`；可用环境变量 `LANDROP_PYTHON` 指定解释器路径。

只预览界面（不启动 Tauri）可运行 `npm run dev`，浏览器打开 `http://127.0.0.1:1424`。此时没有 Tauri 桥接，界面会提示「浏览器预览模式」，只能查看布局，不能传输。

## 测试

```powershell
python -m pytest      # Python：配置、传输核心、API、真实 SFTP 端到端
npm run test          # 前端：store 状态、SSE 解析
npm run build         # TypeScript 类型检查 + 生产构建
```

真实 SFTP 测试使用 `tests/sftp_server.py` 中的进程内服务端，覆盖主机密钥校验、续传偏移、哈希一致性与发布语义，不依赖外部设备。

## 单独运行 Python 服务

调试服务本身时可手工启动。服务只绑定 `127.0.0.1`，所有请求必须带令牌：

```powershell
$env:LANDROP_SERVICE_PORT = "43127"
$env:LANDROP_SERVICE_TOKEN = [Convert]::ToBase64String([Security.Cryptography.RandomNumberGenerator]::GetBytes(48))
python -m landrop.api.app
```

接口一览：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 就绪检查 |
| GET/PUT | `/api/config` | 读写非敏感连接配置 |
| POST | `/api/hostkeys/probe` | 读取远端主机指纹 |
| POST | `/api/hostkeys/trust` | 确认并保存指纹 |
| GET/PUT/DELETE | `/api/credentials[/{username}]` | Windows 凭据管理器中的密码 |
| POST | `/api/transfers` | 创建传输任务 |
| GET | `/api/transfers/{id}` | 任务快照 |
| GET | `/api/transfers/{id}/events` | SSE 进度，支持 `Last-Event-ID` 续接 |
| POST | `/api/transfers/{id}/cancel` | 取消，保留远端 `.part` |
| POST | `/api/transfers/{id}/retry` | 以新任务重试 |

所有接口都要求 `X-LANDrop-Token` 或 `Authorization: Bearer <token>`。密码只在创建/重试请求中短暂传递，不写入日志、快照或 URL。

## 安全要点

- 本机服务仅监听 loopback，非 loopback 客户端与未知 `Origin` 均被拒绝。
- 主机密钥采用 trust-on-first-use：未知主机直接拒绝连接，需用户在界面确认指纹后写入应用私有的 `known_hosts`；指纹变化时拒绝保存。
- 应用私有 `known_hosts` 位于 `%APPDATA%\LANDrop\known_hosts`，不修改用户的 `~/.ssh/known_hosts`。
- 密码持久化仅通过 Windows Credential Manager。
- sidecar 被放入 Windows Job Object（kill-on-close），即使宿主进程崩溃或被强制结束，Python 子进程也会一并退出。

## 文档

- [用户手册](docs/用户手册.md) — 面向使用者的分步操作手册：基础准备、密码认证、密钥认证
- [概要](docs/概要.md) — 一页纸速查：原理、用法、待补功能
- [项目计划](docs/项目计划.md)
- [设计实现思路](docs/设计实现思路.md)
- [技术实现](docs/技术实现.md)
- [应用使用指南](docs/应用使用指南.md)
- [新机部署验证](docs/新机部署验证.md) — 全新机器从零配置到传输验收，含单机自测
- [SSH 密钥生成与配置](docs/密钥生成.md)
