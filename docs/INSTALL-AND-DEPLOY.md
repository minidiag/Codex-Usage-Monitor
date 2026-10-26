# 本地采集与服务器部署方法

适用版本：客户端 0.6.0、服务端 0.3.0、数据库 3。2026-10-26 根据项目现有脚本核对。

## 1. 当前安装在哪里

| 部分 | 当前配置 |
|---|---|
| Windows 程序 | `E:\Codex\Codex-Usage-Monitor` |
| 当前用户状态 | `%LOCALAPPDATA%\CodexUsageMonitor\<安装标识>\state` |
| 服务器程序 | `YOUR_SERVER_HOST` 的 `/opt/codex-usage-monitor` |
| HTTPS 地址 | `https://YOUR_SERVER_HOST/codex-usage` |
| 数据库 | Docker 私有网络内的 PostgreSQL，不开放公网端口 |

本机及当前服务器已部署。日常使用只需打开桌面“Codex 用量统计”，无需重复初始化。

## 2. 已配置用户：启动、停止和检查

在 PowerShell 中执行：

```powershell
Set-Location 'E:\Codex\Codex-Usage-Monitor'
& .\client\Start-UsageMonitor.ps1
```

仅运行后台采集：

```powershell
& .\client\Start-UsageMonitor.ps1 -NoWindow
```

停止当前用户的采集器和窗口，或取消该用户登录自启动：

```powershell
& .\client\Stop-UsageMonitor.ps1
# 仅在需要取消自动启动时执行下一行
& .\client\Remove-Startup.ps1
```

窗口右上角 × 仅关闭窗口，后台继续采集。查看服务器汇总：

```powershell
& .\deploy\Query-Usage.ps1
```

该查询脚本使用 PowerShell 7。正常状态是窗口“已同步”、待传队列为 0、没有错误；“部分覆盖”表示未导入旧历史或存在尚未适配的来源，不等于部署失败。

出现“部分来源暂停”时，点击该提示或右键选择“核实暂停来源…”，查看日志基线、历史检查点和服务器核实结果，并通过受控来源选择恢复。具体操作见 [来源核实与恢复](SOURCE-REVIEW.md)。

## 3. 新电脑或新 Windows 用户安装

### 3.1 准备程序与环境

1. 将本项目的程序源文件放到固定目录，例如 `E:\Codex\Codex-Usage-Monitor`。需包含 `client/` 和 `server/protocol.py` 等项目文件；服务端 tar.gz 包不含 Windows 客户端，不能用它代替完整项目。
2. 安装当前用户可运行的 Python 3.12 或更新版本，同目录需有 `pythonw.exe`；客户端采集运行依赖使用 Python 标准库，无需安装服务端依赖。
3. 需要 Windows .NET Framework WPF 及其 `Framework64\v4.0.30319\csc.exe`。客户端不要求安装 .NET SDK。
4. 本用户已有正常使用的 Codex 及可读取的日志目录。默认读取 `CODEX_HOME`；没有设置时读取本用户的 `~/.codex`。额度读取还需要可用的本机 `codex.exe` 和登录状态。

若共用程序目录，管理员先构建一次窗口，并给目标用户读取和执行程序文件的权限。不要把原用户的 `.local` 账本、凭据或应用数据目录随程序复制给新用户。

### 3.2 管理员在服务器签发独立采集凭据

每个“电脑 + Windows 用户”的采集安装使用独立设备凭据；共享同租户匿名化密钥以便合并同一 Codex 账号。下面命令在服务器的项目目录执行，文件名每次使用新的名字：

```sh
cd /opt/codex-usage-monitor
set -a
. ./deploy/.env
set +a
export DATABASE_URL="postgresql://usage_owner:${OWNER_DB_PASSWORD}@db:5432/codex_usage"
docker compose --env-file deploy/.env -f deploy/compose.yml run --rm -T --no-deps \
  --user 0:0 -e DATABASE_URL -v /opt/codex-usage-monitor/private:/private \
  api python -m server.admin add-device \
  --from-credentials /private/client.json --output /private/collector-02.json
unset DATABASE_URL
```

通过 SSH/SCP 将该 JSON 安全交付到目标 Windows 用户，勿打印或发到聊天中。`provision` 创建新租户，新增已有租户的采集器应使用上面的 `add-device`。

### 3.3 在目标 Windows 用户自己的登录会话中安装

以下解释器和 JSON 路径是示例，按实际位置修改：

```powershell
Set-Location 'E:\Codex\Codex-Usage-Monitor'
& .\client\Build-Window.ps1
& .\client\Install-User.ps1 -CredentialImport 'C:\Private\collector-02.json' -PythonExe 'C:\Users\用户名\AppData\Local\Programs\Python\Python312\python.exe'
& .\client\Start-UsageMonitor.ps1
```

已有窗口运行时先停止，再构建。如果管理员已构建共享 EXE，目标用户可跳过 Build-Window。安装会将遥测凭据用本 Windows 用户 DPAPI 加密，并设置该用户的桌面入口和登录自启动；安装后按交付流程妥善清理明文 JSON，安装脚本不会自行删除它。

已经配置好的用户无需再次传 `-CredentialImport`，重新运行 Install-User 可更新运行环境和快捷方式。现有加密凭据不能跨用户、跨电脑直接复用；导入脚本也不会覆盖已有凭据。

### 3.4 同一 Windows 用户使用多个 Codex 目录

```powershell
& .\client\Stop-UsageMonitor.ps1
& .\client\Install-User.ps1 -PythonExe 'C:\Path\To\Python312\python.exe' -CodexHome 'D:\Codex-A','D:\Codex-B'
& .\client\Start-UsageMonitor.ps1
```

不需要列出或限制 Codex 账号。独立目录分别观察身份与额度；同一目录顺序换号也会被观察。账号切换边界不清或无法确认创建者时，增量保留为未归属，不猜测。多个 Windows 用户需分别登录、安装并运行；当前实现不是跨用户系统服务。

## 4. 服务端首次部署

### 4.1 适用范围与准备

当前脚本针对 Linux x86_64、Docker Engine + Compose 插件、Python 3、curl，以及固定路径 `/opt/codex-usage-monitor`。当前主机约 2 GB 内存。入口需要 Nginx 和可由客户端正常校验的 HTTPS 证书；公网只需提供 HTTPS，8190 仅绑定本机，数据库端口不发布。

`generate_env.py` 首次运行会检查两个指定镜像是否已经在本机，因此先准备：

```sh
docker pull docker.m.daocloud.io/library/python:3.12-slim
docker pull docker.m.daocloud.io/library/postgres:18-alpine
```

当前部署固定为 Python 3.12，因此下述 wheel 为 CPython 3.12 / Linux x86_64。若换 CPU 架构或 Python 镜像，必须同时调整 wheel 平台与版本。

### 4.2 在 Windows 制作服务端部署包

在项目目录中执行；若没有该虚拟环境，先用自己的 Python 3.12 创建：

```powershell
Set-Location 'E:\Codex\Codex-Usage-Monitor'
py -3.12 -m venv .local/venv
& ./.local/venv/Scripts/python.exe -m pip download --platform manylinux2014_x86_64 --python-version 312 --implementation cp --abi cp312 --only-binary=:all: -r server/requirements-test.txt -d .local/wheels
& ./.local/venv/Scripts/python.exe deploy/package.py
Get-FileHash .local/codex-usage-monitor-0.3.0.tar.gz -Algorithm SHA256
```

已有虚拟环境时跳过创建。打包脚本输出压缩包、SHA256 和文件清单；不包含服务器 `.env`、private 凭据或本机状态。

先在目标服务器创建 `/opt/codex-usage-monitor`。从 Windows 直连传输，SSH 不使用代理：

```powershell
scp -F NUL -o ProxyCommand=none -o ProxyJump=none .local/codex-usage-monitor-0.3.0.tar.gz .local/codex-usage-monitor-0.3.0.tar.manifest.json root@YOUR_SERVER_HOST:/opt/codex-usage-monitor/
ssh -F NUL -o ProxyCommand=none -o ProxyJump=none root@YOUR_SERVER_HOST
```

服务器核对压缩包 SHA256 与本机输出一致，再解压；新服务器上的首次解压示例：

```sh
cd /opt/codex-usage-monitor
sha256sum codex-usage-monitor-0.3.0.tar.gz
tar -xzf codex-usage-monitor-0.3.0.tar.gz
sh deploy/bootstrap.sh
sh deploy/test-database.sh
curl -fsS http://127.0.0.1:8190/health/ready
```

bootstrap 自动生成仅本机可读的 `.env`，固定镜像 digest、创建数据库密码、构建 API、执行迁移与权限初始化，并仅在首次生成 `private/client.json`。测试使用独立临时数据库，运行后清理，不向生产账本写入测试数据。

**换成另一台服务器时需先调整配置。** 当前 bootstrap 首次签发凭据的默认 base_url 指向 `https://YOUR_SERVER_HOST/codex-usage`；首次运行前，应在其 `server.admin provision` 命令上加 `--base-url https://你的实际地址/codex-usage`。部署包内的 `enable-nginx.py` 也针对当前服务器既有 benchlink 配置，不是通用 Nginx 安装器。

### 4.3 配置 HTTPS 入口

当前阿里云主机已配置，可用：

```sh
python3 deploy/enable-nginx.py
curl -fsS https://YOUR_SERVER_HOST/codex-usage/health/live
curl -fsS https://YOUR_SERVER_HOST/codex-usage/health/ready
```

全新服务器应先配置自己的 Nginx HTTPS server 和证书，然后将 `deploy/nginx/codex-usage.location.conf` 的 location 配置包含到相应 HTTPS server 内。执行 `nginx -t` 通过后再 reload。使用 IP 时证书须覆盖该 IP；不能用关闭客户端证书校验替代 HTTPS 配置。安全组和主机防火墙需放行 HTTPS，SSH 按管理需求开放。

首次 `private/client.json` 为首台采集器凭据；后续电脑或用户使用第 3 节的 add-device。保留原文件作为租户匿名化密钥的安全来源，不向所有用户重复分发同一设备凭据。

## 5. 已部署服务器升级与日常维护

当前主机已经部署，更新时先备份，再上传并核验新包、解压、构建和迁移。不要重建 `.env`、private 或数据库卷：

```sh
cd /opt/codex-usage-monitor
sh deploy/backup.sh
# 完成新包的 SHA256 核对与解压后
sh deploy/bootstrap.sh
sh deploy/test-database.sh
docker compose --env-file deploy/.env -f deploy/compose.yml ps
curl -fsS http://127.0.0.1:8190/health/ready
```

另保存升级前源码及配置备份；备份目录 `backups/`、私有配置 `deploy/.env`、`private/client.json` 都需要妥善保管。不得执行 `docker compose down -v` 删除数据卷。日常备份由已配置的 `codex-usage-backup.timer` 运行；当前没有异地副本。

查看有限条 API 日志：

```sh
docker compose --env-file deploy/.env -f deploy/compose.yml logs --tail 100 api
```

查看项目数据与周期统计：

```powershell
& .\deploy\Query-Usage.ps1 -ListIdentities
& .\deploy\Query-Usage.ps1 -GroupBy account,principal -AsJson
& .\deploy\Query-Usage.ps1 -QuotaCycles -Account '<完整匿名账号键>' -WindowsUser '<完整匿名用户键>'
```

客户端加密凭据的授权范围决定查询可见数据；新用户凭据默认限制为其 Windows 主体。窗口始终只显示当前 Windows 用户。进一步说明见 [多账号操作](MULTI-ACCOUNT-OPERATIONS.md)、[周期口径](QUOTA-CYCLES.md) 与 [维护手册](../deploy/RUNBOOK.md)。
