# Codex Usage Monitor

在 Windows 采集 Codex 数值检查点，由自有服务器去重、派生增量并汇总。当前已部署 **客户端 0.8.1 / 服务端 0.5.0 / 数据库 schema v5**，核对日期 2026-10-29。

## 日常使用

打开桌面“Codex 用量统计”。窗口固定展示当前 Windows 用户，按 Codex 账号切换；关闭窗口后后台继续采集，停止操作使用右键“停止采集并退出”。

- 四周期卡片：最近 5 小时滚动窗口，以及北京时间今日、本周、本月。
- USAGE：官方额度剩余比例、倒计时和真实重置周期内的已采集 Token；与四周期卡片分别计算。
- 活跃会话：当前用户、所选账号已确认分段，不额外加入周期总量。
- 采集来源：支持来源变化记录、压缩核实及受控恢复；未适配来源保守标记部分覆盖。
- 网页：[用量仪表盘](https://YOUR_SERVER_HOST/codex-usage/)，按账号查询最近 24 小时或近 7/30/90 日，支持图形/表格、指标切换和 CSV。

窗口底部点击 **“网页凭据”**，或右键选择 **“网页仪表盘登录…”**，复制用户只读凭据后打开网页粘贴登录，无需单独执行脚本。程序先校验该凭据属于当前 Windows 用户；管理员级或其他用户范围的凭据不能通过此入口复制。

## 账号名称和权限

展示名称通过独立接口同步到服务器，网页优先显示本地备注，其次同步名称，最后回退为匿名账号键。头像、对话正文、任务标题、日志路径及 Codex 登录凭据不上传。名称不参与身份认证、用量归属或去重。

用户级 read token 仅查看绑定 Windows 主体的数据；换电脑打开网页不改变授权范围。管理员使用同租户的 tenant 级 read token 查看所有已接入账号及用户。只读与可见范围是两个维度，详见 [网页与权限说明](docs/DASHBOARD.md)。

总 Token 为输入加输出；缓存输入、推理输出为子集，不重复相加。统计仅代表已采集账本，不代表完整官方账单，始终按部分覆盖解释。

## 文档入口

- [安装与部署](docs/INSTALL-AND-DEPLOY.md)：当前用户启动、新用户接入、服务器安装和升级。
- [Windows 客户端](client/README.md) · [客户端发布清单](client/RELEASE.json)
- [网页与凭据权限](docs/DASHBOARD.md) · [本机账号名称与头像](docs/LOCAL-ACCOUNT-UI.md)
- [多用户与账号归属](docs/MULTI-ACCOUNT-OPERATIONS.md) · [额度周期统计](docs/QUOTA-CYCLES.md)
- [来源核实与恢复](docs/SOURCE-REVIEW.md) · [Token 记录机制核查](docs/TOKEN-RECORDING-MECHANISM.md)
- [运维手册](deploy/RUNBOOK.md) · [部署验证历史](deploy/STATUS.md) · [服务端发布清单](deploy/RELEASE.json)
- [项目交接](HANDOFF.md) · [原始 DD 设计草案](docs/Codex-Telemetry-DD/DD.md)

服务端存活状态位于 [health/live](https://YOUR_SERVER_HOST/codex-usage/health/live)，根地址现为网页而非 JSON。PowerShell 查询工具 `deploy/Query-Usage.ps1` 需要 PowerShell 7。

## 当前验证和限制

客户端 152 项 Python 测试、20 项周期显示检查及完整 WPF 构建通过；服务端 61 项测试在真实 PostgreSQL 18.6 独立库通过。schema v5 备份恢复、重复迁移和正式迁移旧表指纹校验通过；真实名称同步、趋势和两个额度周期与账本对账通过。凭据入口真实用户范围校验及界面复制已核对。

网页交互已在本地验证；内置浏览器访问公网超时，未记为生产浏览器验收通过。本机独立 HTTPS 和数据接口验证通过。跨真实 Windows 用户、跨机器、负载、异地备份、完整历史和所有日志格式覆盖尚未完成。

## 目录

`client/` 为 Windows 采集器及原生 WPF 窗口；`server/` 为协议、账本、元数据、趋势 API 和网页；`deploy/` 为部署、备份与运维工具。每用户凭据和账本在 `%LOCALAPPDATA%/CodexUsageMonitor/<安装标识>/`，`.local/` 保存本机生成产物，不作为公开源代码发布内容。
