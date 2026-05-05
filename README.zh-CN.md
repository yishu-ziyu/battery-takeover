![Battery Takeover release preview](./docs/assets/release/release-hero.png)

# 电池接管

语言：[English](./README.md) | 简体中文

电池接管（Battery Takeover）是一个面向 macOS 的本地电池监控与阈值控充工具。它适合长期插电使用 MacBook 的场景，用一种简单、可检查的方式减少电池长时间停留在 100% 的时间。

项目当前提供轻量命令行工具、本地 Dashboard、可选 LaunchAgent 自启、日报复盘，以及 macOS `.pkg` 安装包。

## 功能

- **电池采样**：记录当前电量、外接电源状态、充电状态、循环次数与容量信号。
- **阈值策略**：按可配置的上限与下限停止或恢复充电，例如 `92 / 88`。
- **本地 Dashboard**：查看当前状态、最近 24 小时历史、运行状态与最近动作。
- **一键模式切换**：可在 Dashboard 中开启或关闭项目电池管理。
- **只读降级**：写入后端不可用或动作失败时，继续保留监控能力。
- **日报复盘**：基于本地样本和动作记录生成每日摘要。
- **LaunchAgent 支持**：可在登录后自动启动 agent。
- **安装包构建**：提供 macOS `.pkg`，方便本地安装。

## 系统要求

- macOS 15.x
- Apple Silicon Mac
- Python 3.11+
- 可用的充电后端：
  - `batt` 是优先支持的后端
  - `battery` 保留为备选后端

## 安装

### 方式一：从 GitHub Releases 安装

从下面地址下载最新的 `battery-takeover-<version>-installer.pkg`：

[https://github.com/yishu-ziyu/battery-takeover/releases](https://github.com/yishu-ziyu/battery-takeover/releases)

下载后双击安装包完成安装。

安装包会配置：

- 运行副本
- LaunchAgent
- 名为 `电池接管.app` 的桌面入口
- 本地 Dashboard 入口

当前安装包尚未签名，也尚未 notarized。首次安装时，macOS 可能显示安全提示。

### 方式二：从源码运行

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

## 快速开始

检查当前环境是否可用：

```bash
./btake --config ./config/default.toml doctor
```

初始化运行目录：

```bash
./btake --config ./config/default.toml init
```

采集一次样本并查看策略判断：

```bash
./btake --config ./config/default.toml sample
./btake --config ./config/default.toml enforce --dry-run
```

执行策略：

```bash
./btake --config ./config/default.toml enforce
```

启动本地 Dashboard：

```bash
./btake --config ./config/default.toml dashboard --open
```

使用统一脚本：

```bash
./control.sh start
./control.sh status
./control.sh stop
```

## Dashboard

Dashboard 默认运行在本机地址：

[http://127.0.0.1:8775](http://127.0.0.1:8775)

Dashboard 提供：

- 当前电量与运行模式
- 最近 24 小时电量曲线
- 最近一次采样与最近一次动作
- 当前充电后端
- LaunchAgent 状态
- 停充与恢复阈值
- 项目电池管理开关
- 手动执行策略

![Dashboard screenshot](./docs/assets/screens/dashboard-live.png)

## 充电模式

电池接管有两种明确运行模式：

### 项目电池管理：开启

策略引擎会按配置的停充阈值和恢复阈值执行控充。这通常适合长期插电办公，例如：

```text
stop at 92%
resume at 88%
```

### 项目电池管理：关闭

项目会清除自己设置的充电限制，并把控制权交还给系统和底层后端。这适合出门前希望把电脑充到 100% 的场景。

在 Dashboard 中，**保存设置并立即应用** 会持久化配置，并立刻应用当前选择的模式。

## 日报

生成本地日报：

```bash
./btake --config ./config/default.toml report daily
```

日报会写入配置中的 reports 目录。

## 开机自启与桌面入口

安装 LaunchAgent：

```bash
./install_agent_launchd.sh
```

安装桌面入口：

```bash
./install_desktop_app.sh
```

生成的 app 入口：

- `~/Applications/电池接管.app`
- `~/Desktop/电池接管.app`

卸载：

```bash
./uninstall_desktop_app.sh
./uninstall_agent_launchd.sh
```

## 故障排查

如果 `doctor` 提示后端不可用，先直接检查后端：

```bash
batt status
```

常见情况：

- `batt daemon is not running`：启动或重新安装 `batt` daemon。
- socket 或权限错误：按后端文档修复 daemon 权限。
- 只读模式：采样仍可继续，但电池接管不会尝试写入动作，直到环境重新满足条件。

## 范围与安全说明

电池接管运行在 macOS 与所配置后端暴露的能力范围内。它不宣称提供硬件级电池旁路，也不宣称提供硬件级电源切换能力。

当前实现是通过后端在配置阈值处停止或恢复充电。当后端缺失、不可用或返回错误时，项目会退回监控模式，而不是强行控制。

Dashboard 只绑定本机 loopback 地址。它是本地控制界面，不是面向局域网或公网暴露的服务。

## 开发

运行测试：

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

构建安装包：

```bash
./build_macos_installer.sh
```

默认安装包输出：

```bash
./dist/battery-takeover-<version>-installer.pkg
```

发布页资源：

- [发布页 HTML](./docs/release-page.html)
- [桌面预览](./docs/assets/release/release-page-desktop.png)
- [移动端预览](./docs/assets/release/release-page-mobile.png)

## 文档

- [Changelog](./CHANGELOG.md)
- [产品文档](./docs/产品文档.md)
- [开发日志](./docs/开发日志.md)
- [调研基线](./调研-开源与产品基线.md)

## License

MIT
