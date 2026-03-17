# Changelog

All notable changes to this project will be recorded in this file.

## [0.2.1] - 2026-03-17

### Added
- Dashboard 增加“项目电池管理”开关，用于在项目阈值控充与系统默认充电之间切换。
- 配置更新路径增加 `enabled` 持久化与对应测试覆盖。

### Changed
- Dashboard 保存设置后立即应用，不再仅写入配置文件等待下一轮调度。
- README 与发布说明改写为更克制的技术说明风格，强调边界、依赖与运行模式。

### Fixed
- 关闭项目管理时会主动清除已有充电限制，避免阈值修改后行为仍被旧状态锁定。

## [0.2.0] - 2026-03-06

### Added
- `macOS .pkg` 安装包分发链路。
- 安装后自动部署运行副本、LaunchAgent 与桌面入口。
- GitHub Actions 构建安装包工作流。
