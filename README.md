![电池接管封面](./docs/assets/cover.svg)

# 电池接管（Battery Takeover）

`电池接管` 是一个面向 macOS 的电池监控与阈值控充工具。它的目标不是宣称“物理旁路电池”，而是在系统与后端允许的范围内，把电池维持在更合理的区间，并把整个过程记录下来，方便回看和审计。

当前版本已经打通完整 MVP：分钟级采集、`92/88` 阈值控充、日报复盘、开机自动运行、轻量 Dashboard，以及可点击的桌面入口。

## 能力概览
- 分钟级采集电池、电源、健康状态
- 按阈值执行停充/恢复策略
- 自动记录样本、动作和运行状态
- 连续失败自动降级到只读监控
- 提供 24 小时曲线和动作审计
- 支持 LaunchAgent 开机自动运行
- 支持轻量桌面 App 点击启动

## 现实边界
- macOS 没有公开官方 API 允许普通应用强制“只走适配器不走电池”。
- 本项目当前实现的是“停止继续充电并维持区间”，不是物理层面的电源旁路。
- 真实控充能力依赖第三方后端，当前优先兼容 `batt`。

## 项目文档
- [产品文档](./docs/产品文档.md)
- [开发日志](./docs/开发日志.md)
- [调研基线](./调研-开源与产品基线.md)

## 环境要求
- macOS 15.x
- Apple Silicon
- Python 3.11+
- 可用的 `batt` 或 `battery` 后端

## 快速开始

### 1. 安装依赖
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

### 2. 环境体检
```bash
./btake --config ./config/default.toml doctor
```

### 3. 初始化
```bash
./btake --config ./config/default.toml init
```

### 4. 手动验证采样与策略
```bash
./btake --config ./config/default.toml sample
./btake --config ./config/default.toml enforce --dry-run
./btake --config ./config/default.toml enforce
```

### 5. 启动本地面板
```bash
./btake --config ./config/default.toml dashboard --open
```

### 6. 推荐统一入口
```bash
./control.sh start
./control.sh status
./control.sh stop
```

## 开机自动运行
安装 LaunchAgent：

```bash
./install_agent_launchd.sh
```

验证：

```bash
launchctl list | rg com.battery.takeover.agent
tail -n 60 "$HOME/Library/Application Support/BatteryTakeover/app/logs/launchd.err.log"
```

## 桌面入口
安装轻量桌面 App：

```bash
./install_desktop_app.sh
```

安装后会生成：
- `~/Applications/电池接管.app`
- `~/Desktop/电池接管.app`

卸载：

```bash
./uninstall_desktop_app.sh
./uninstall_agent_launchd.sh
```

## 常用命令
```bash
./btake --config ./config/default.toml doctor
./btake --config ./config/default.toml status
./btake --config ./config/default.toml report daily
batt status
```

## 已知问题与说明
- `batt` 必须保证 daemon 正常运行，否则只能降级到只读监控。
- 如果 `doctor` 显示 `batt daemon is not running`，优先检查：

```bash
sudo brew services start batt
batt status
```

- 如果出现权限或 socket 问题，需要按 `batt` 官方方式修正 daemon 权限配置。

## 测试
```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## License
MIT
