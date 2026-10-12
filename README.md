# 期权卖方信号监控（Pages 版）

数据由 GitHub Actions 定时刷新（北京时间 9:35~15:35 每30分钟 + 16:05 收盘补充），
页面带访问密码（AES-GCM 加密 data.bin，密码由仓库 Secret `PAGE_PASSWORD` 控制）。

## 仓库结构
- `docs/index.html` 静态页面（含密码门）
- `docs/data.bin` 加密后的最新评级数据（Actions 自动更新，勿手改）
- `docs/history.json` 历史信号累积（Actions 自动更新）
- `build_data.py` 取数脚本（复用私有主仓库 etf-option-monitor 的 monitor 逻辑）
- `encrypt_data.py` 数据加密脚本
- `.github/workflows/refresh.yml` 定时刷新工作流

## 所需 Secrets（Settings → Secrets and variables → Actions）
| 名称 | 用途 |
|---|---|
| `GH_PAT` | GitHub token，用于 Actions 拉取私有主仓库 etf-option-monitor |
| `PAGE_PASSWORD` | 页面访问密码（未设置时默认 miraseed2026） |
| `WECOM_WEBHOOK` | 可选，企业微信机器人 webhook，新信号推送 |

## 修改密码
改 `PAGE_PASSWORD` Secret 即可，下次刷新后新密码生效（旧密码立即失效，因为每次加密用新盐）。
