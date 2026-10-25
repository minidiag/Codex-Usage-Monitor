# 多用户与多账号：0.4.0 使用和维护

2026-10-26。本文保留 0.4.0 多用户采集与服务器接入说明。当前客户端已升为 0.6.0，窗口固定为当前 Windows 用户，并使用真实头像和明文姓名切换账号，具体以 [本地账号界面说明](LOCAL-ACCOUNT-UI.md) 为准。服务器已升为 0.3.0、数据库版本 3，兼容协议 1.0/1.1；新增额度周期元数据及持久汇总，见 [周期统计](QUOTA-CYCLES.md)。第二个真实 Windows 登录会话尚未验收。

## 已实现的统计关系

不配置“哪些 Windows 用户可以使用哪些 Codex 账号”的名单。一个 Windows 用户可以使用任意多个账号，同一账号可以由任意多个已接入的 Windows 用户使用。采集凭据仍需独立安装和鉴权；这是遥测数据的访问控制，不限制 Codex 账号的实际使用。

服务器分别记录匿名账号、Windows 用户、电脑和上报实例：

- 同一 Codex 登录用户、同一工作空间在不同 Windows 用户下使用，账号键相同，账号用量合并。
- 同一工作空间内的不同 Codex 登录用户不会混在一起；同一登录用户切换工作空间分开统计。
- Windows 主体由电脑标识和当前进程的 Windows SID 匿名化生成；同名 Windows 用户不自动合并。不同电脑上的 Windows 用户分别统计。
- 上报实例身份与逻辑事件身份分离。复制日志、重试、跨实例上传不会创建第二份数值，也不会把已确认执行用户改为副本上传者。

## 窗口操作

本节记录 0.4.0 的管理范围选择；0.5.0 已移除本地 Windows 用户/电脑选择，管理员跨用户统计通过独立服务器查询工具进行。

顶部新增两个选择框，条件取交集：

1. **账号**：全部账号（含未归属）、未归属账号、自动发现的具体账号。默认使用匿名账号键的前 8 位作为名称。
2. **来源**：全部授权用户与电脑、本 Windows 用户、本机全部 Windows 用户、具体 Windows 用户或电脑，以及未归属 Windows 用户。

选择后，四周期卡片从服务器重新查询。等待期间显示“—”，不会用旧范围数字冒充新范围。暂停开关与筛选条件分别保存，互不覆盖。

“本用户活跃会话”始终只显示本 Windows 用户能够读取的会话详情。全部账号视图沿用整段会话累计；选择具体账号或未归属后，数字改为本机账本中已采集的相应账号分段累计。它不是账号全量历史，也不额外加进四周期。选择其他用户时，提示其会话详情未上报；不会上传标题来填充远端列表。

账户额度按所选账号匹配本机各数据目录的缓存。未在本机读取到该账号额度时显示不可用，不展示另一个账号的比例。全部账号视图标明“本机当前登录”，不对多个账号的百分比求和。不同 Windows 用户使用同一账号时，也不能把读到的同一份剩余比例相加。

## 自动归属与诚实的未知值

当前元数据适配器只识别本机 ChatGPT 登录资料中的账号/工作空间与登录用户标识，并用只读的会话索引核对创建者。标识经共享匿名化密钥 HMAC 后才能出模块。登录文件中的令牌只在内存中用于读取身份元数据，不记录、不上传、不用于服务器认证，不验证为可信财务身份。接口说明参考 [OpenAI Docs：Codex App Server](https://learn.chatgpt.com/docs/app-server)；本地登录文件及 SQLite 是当前版本适配器，格式改变时保守降级。

每约 5 秒观察一次。只有前后检查点均位于连续观察区间、登录文件未变化、会话创建者与观察到的登录身份一致时，才赋予账号。Windows 用户和电脑归属也不回填观察之前的旧数据。

以下情况留为未归属，而不是猜测或按时间比例分摊：

- 升级前历史、启动前积压或观察起点之前的增量区间。
- 采集间隔超过 20 秒、长时间暂停、睡眠、重启后的跨界区间。
- 登录文件变化或切换过程中跨界的区间；即使只是刷新令牌，也可能保守留下一段未知。
- 多个目录对同一逻辑会话提供冲突身份，或在原账号创建的旧会话中切换到另一个账号继续使用。
- API key、未知认证格式、缺少创建者元数据或无法核对身份。不会用 API key 或邮箱生成账号键。

账号未知但已连续确认本机执行主体的区间可以保留 Windows 用户归属；无法确认执行主体时也保持未知。总量始终包含这些数值。旧账本保持原数值和事件 ID，不将历史自动归到升级时登录的账号。

## 同一 Windows 用户配置多个数据目录

默认扫描本用户的 `CODEX_HOME`，未设置时为 `~/.codex`。使用多个独立 Codex 数据目录时，在本用户运行：

```powershell
& 'E:\Codex\Codex-Usage-Monitor\client\Install-User.ps1' `
  -PythonExe 'C:\Path\To\Python312\python.exe' `
  -CodexHome 'C:\Path\To\Codex-A','C:\Path\To\Codex-B'
```

目录是示例，请换成实际目录；不需要传账号名、邮箱或登录令牌。配置保存为当前用户的 `state/sources.json`，重启采集器生效。不同目录分别读取身份与额度，不重新登录账号。共用目录顺序切换会自动观察；共用目录并发账号或跨账号续用旧会话不能保证完整归属，建议使用独立目录。

## 另一个 Windows 用户首次接入

管理员为每个 Windows 用户的采集安装生成独立遥测凭据，沿用同租户的匿名化密钥。不能把现有 Windows DPAPI 加密文件复制给另一个用户，也不能多人共用一个设备写凭据。

服务器管理员示例（在 `/opt/codex-usage-monitor`，不打印环境变量内容）：

```sh
set -a
. ./deploy/.env
set +a
export DATABASE_URL="postgresql://usage_owner:${OWNER_DB_PASSWORD}@db:5432/codex_usage"
docker compose --env-file deploy/.env -f deploy/compose.yml run --rm -T --no-deps \
  --user 0:0 -e DATABASE_URL -v /opt/codex-usage-monitor/private:/private \
  api python -m server.admin add-device \
  --from-credentials /private/client.json --output /private/collector-02.json
```

输出文件不得覆盖已有凭据。安全交付到目标 Windows 用户后，在该用户会话中运行：

```powershell
& 'E:\Codex\Codex-Usage-Monitor\client\Install-User.ps1' `
  -CredentialImport 'C:\Path\To\collector-02.json' `
  -PythonExe 'C:\Path\To\Python312\python.exe'
```

该用户需要可读取的项目程序文件，以及自己可运行的 Python 3.12 或更新版本（同目录含 pythonw.exe）。不能依赖其他 Windows 用户私有目录中的解释器。安装脚本验证运行环境、用当前用户 DPAPI 加密、配置自己的桌面与登录启动入口；交付的明文文件需按凭据交付流程妥善收回或清理，脚本不会擅自删除用户指定文件。

新采集器第一次启动时自动登记该 Windows 用户和电脑，之后相同设备凭据不能改绑另一个执行主体。无需登记或批准它可以使用哪些 Codex 账号。新发放的查询凭据默认只读本 Windows 主体的数据；现有管理员查询凭据具有租户总览权限。跨用户总览由查询凭据决定，不是任意客户端默认拥有全部数据。

## 每用户独立状态

程序仍位于项目目录，EXE 为 `.local/client/CodexUsageMonitor.exe`。运行状态已移到：

```text
%LOCALAPPDATA%\CodexUsageMonitor\<项目安装标识>\
  client.credentials          当前用户 DPAPI 加密
  state\collector.sqlite3     数值账本、游标与待传队列
  state\status.json           本机窗口缓存
  state\control.json          本用户暂停与筛选
  state\sources.json          可选的额外数据目录
  state\runtime.json          本用户 pythonw.exe 路径
  state\quota-cache.json      首个目录额度
  state\quotas\...            其他目录额度缓存
```

相同程序在不同 Windows 用户下拥有不同的可写目录和锁。当前用户的旧账本通过 SQLite 备份接口迁移，原基线、事件与队列保留；旧文件作为备份保留，并将当前用户拥有的旧敏感文件限定为该用户、SYSTEM 和管理员可访问，程序 EXE 仍可共用。首次导入其他用户凭据不会复制原用户账本。

不同 Windows 用户必须各自在其会话内启动采集器。没有登录、已注销或离线的来源不会实时上报；登录后补传。未实现后台跨用户系统服务，不自动读取其他用户的登录资料。

## 服务器查询

新增只读 `/v2/identities` 和 `/v2/usage/summary`，筛选参数 `account`、`principal`、`machine` 为完整匿名键，或 `unassigned`。`group_by=account,principal,machine` 可选择一个或多个维度；缺少对应归属的历史以 JSON null 分组。过滤、授权和事件时间窗口在同一查询快照中生效。

本机查询工具已支持：

```powershell
& '.\deploy\Query-Usage.ps1' -ListIdentities
& '.\deploy\Query-Usage.ps1' -GroupBy account,principal
& '.\deploy\Query-Usage.ps1' -Account unassigned
& '.\deploy\Query-Usage.ps1' -Account '<完整账号键>' -WindowsUser '<完整用户键>' -AsJson
```

仍支持旧 `/v1/usage/summary`，新受限查询凭据通过旧接口也不能越过用户范围。旧客户端可以继续上传 1.0 数值；新客户端的归属为 1.1 白名单字段。数值语义哈希保持 1.0 的稳定定义，归属不改变去重键。重复上传的不同归属声明只记录来源观测，不改写已接收的首份事件声明。当前不提供自动历史认领、归属修订界面或远端活跃标题。

## 部署和验证记录

生产升级前创建数据库备份 `backups/pre-upgrade-0.2.0.dump` 和校验文件，代码备份 `private/pre-upgrade-0.2.0-source.tar.gz`。迁移新增表和可空列，不重写已有计数。Docker 新镜像 `codex-usage-monitor:0.2.0`；COPY 指定应用用户拥有代码，保证新迁移目录可读。旧 0.1.0 镜像保留，可用 `USAGE_IMAGE=codex-usage-monitor:0.1.0` 回退 API，保留新增表即可，不应删除生产数据。

26 项服务端协议、HTTP 边界及独立真实 PostgreSQL 测试通过；52 项客户端测试及 14 项原生额度显示检查通过。覆盖共享账号、单用户多账号、不同用户、组合查询、复制重传、未知边界、查询越权、登录切换、跨工作空间、API key 降级、离线声明不变、DPAPI 和用户状态隔离。未在生产账本插入合成用量。

本机真实账号归属和公网查询已验证，所有账号/用户/电脑分组之和与总量一致。2026-10-26 01:58:48 北京时间暂停且队列为空时，147 个已确认检查点推导的四周期总量均为 29,284,990，与服务器逐字段一致。此值只是验收快照，之后持续增长。窗口已核对账号及本机多用户范围选择、完整数字、账号分段会话和额度显示。

当前真实联调只有一个 Windows 用户和一个 Codex 登录账号。跨 Windows 用户及多账号关系已用隔离合成测试验证，仍需要在其他真实 Windows 用户完成凭据导入与登录启动验收；不能把合成覆盖描述为已完成多用户实机演练。
