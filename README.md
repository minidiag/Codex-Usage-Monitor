# Codex Usage Monitor

在 Windows 采集 Codex 的数值用量，并由用户自己的服务器去重和汇总最近 5 小时、今日、本周、本月 Token。项目包含设计文档和首版服务端。

## 已部署的服务端

- 地址：<https://YOUR_SERVER_HOST/codex-usage>
- [存活检查](https://YOUR_SERVER_HOST/codex-usage/health/live)，[数据库就绪检查](https://YOUR_SERVER_HOST/codex-usage/health/ready)。
- 服务端：Python 3.12、Starlette、Psycopg、独立 PostgreSQL 18.6、Docker Compose，沿用现有 Nginx 的 IP 证书。
- 六个核心接口已实现；查询和上报分别使用独立 Bearer 凭据。
- 本机查询凭据使用当前 Windows 账户加密，0.4.0 起保存在用户应用数据目录，详见多账号实施说明；不在代码或文档中公开。

在 PowerShell 7 查询：

```powershell
& 'E:\Codex\Codex-Usage-Monitor\deploy\Query-Usage.ps1'
```

加 `-AsJson` 可获取原始查询结果。脚本直连服务器，不使用系统 HTTP 代理，正常校验 HTTPS 证书。

**Windows 采集器与独立四周期悬浮窗已于 2026-10-26 接入并运行。** 双击桌面“Codex 用量统计”可打开；登录时自动启动，关闭窗口后后台继续采集。服务端已接收到本机真实数值增量，窗口与服务器对账一致。首次启用前的历史未导入，压缩补充用量和子代理等未适配来源仍显示部分覆盖。详见 [Windows 客户端说明](client/README.md)。

客户端 0.2.0 已增加 **USAGE 剩余额度与重置倒计时**，直接读取本机 Codex 账户的官方限额。原版 Codex-Monitor v2.1.4 已按用户要求卸载；新应用独立提供统计与额度显示。

客户端 0.3.0 增加 **活跃会话列表**，参照原版从本机会话索引及任务事件识别活跃状态，显示标题、模型与整段会话累计 Token，约每 5 秒刷新。这是本机视图，包含该会话启用前历史；不并入四周期增量，列表也不上传服务器。详见客户端说明。

客户端 0.6.0 标题为 **Codex**，真实头像与明文账号名居中显示，通过下拉列表切换账号。窗口固定显示当前 Windows 用户的数据，匿名键仅用于后台关联与统计；明文资料和头像仅保存在本机。见 [本地头像账号界面](docs/LOCAL-ACCOUNT-UI.md)。服务器 0.3.0 保持多 Windows 用户、多账号和电脑的匿名分组能力；新用户接入方式见 [实施说明](docs/MULTI-ACCOUNT-OPERATIONS.md)。

USAGE 的 **5 小时额度（当期累计 Token）**、**每周额度（当期累计 Token）** 按官方重置周期统计，并将周期定义及分项累计保存在服务器。与上方滚动/自然周期分别计算。详见 [额度周期统计](docs/QUOTA-CYCLES.md)。

## 文档和实现

- [本地采集与服务器部署方法](docs/INSTALL-AND-DEPLOY.md)：当前用户启动、新电脑/新用户安装、服务器首次部署与升级。
- [DD 0.1](docs/Codex-Telemetry-DD/DD.html)：完整架构和设计。
- [部署与维护说明](deploy/RUNBOOK.md)：配置、凭据、备份、回滚和实施差异。
- [部署验证记录](deploy/STATUS.md)：已执行的验证及当前状态。
- `server/`：校验、事务归并、查询、管理工具和测试。
- `deploy/`：部署、Nginx 接入、备份和本机查询脚本。
- `client/`：Windows JSONL 适配、SQLite 游标与 Outbox、后台采集、原生 WPF 悬浮窗和自启动。

最近 5 小时是滚动窗口；今日、本周和本月以北京时间自然日、周一和每月 1 日为起点。总量为输入加输出，缓存输入和推理输出分别是其子集，不重复相加。服务端只接收白名单数值字段，不接受对话正文、标题和文件路径。

## 验证

当前客户端 70 项测试、15 项账号显示检查、14 项额度显示检查和 11 项周期显示检查通过；服务端 0.3.0 的 34 项测试通过。实际两个额度周期与本地账本逐字段对账一致。真实头像、姓名和窗口账号切换已核对，上报不包含明文展示资料。真实联调仍为一个 Windows 用户、一个 Codex 账号；其他用户需独立导入遥测凭据，跨用户实机登录、完整格式覆盖、跨机器和负载压测仍未完成。
