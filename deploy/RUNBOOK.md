# 服务端部署与维护

## 当前部署

安装与部署请先按 [当前操作步骤](../docs/INSTALL-AND-DEPLOY.md) 执行，区分现有主机升级与新主机首次安装。

2026-10-26 已升级服务器 0.3.0 / 数据库版本 3，并接入 Windows 客户端 0.6.0。新增 POST/GET `/v2/usage/quota-cycles` 保存与查询实际额度重置周期的累计用量，详见 [周期说明](../docs/QUOTA-CYCLES.md)。多账号与多 Windows 用户的当前操作以 [实施说明](../docs/MULTI-ACCOUNT-OPERATIONS.md) 为准；下文首版记录保留为历史。新增 `/v2/identity`、`/v2/identities`、`/v2/usage/summary`，支持匿名账号/用户/电脑过滤和分组，旧协议继续兼容。镜像可由 `USAGE_IMAGE` 选择，默认 `codex-usage-monitor:0.3.0`。

主机为用户指定的 `YOUR_SERVER_HOST`，Alibaba Cloud Linux 3，约 2 GB 内存。服务根目录 `/opt/codex-usage-monitor`，Compose 项目名 `codex-usage-monitor`。

公网基地址 `https://YOUR_SERVER_HOST/codex-usage`。Nginx 终止 TLS，转发到 `127.0.0.1:8190`；数据库不发布主机端口，只连接项目私有网络。API 另接 edge 桥接网络用于回环地址端口发布。此主机 Docker 26 在只连接 internal 网络时未实际建立端口转发，已通过增加 edge 网络解决。

现有 `/` 和 `/ota/` 路由继续使用原配置。仅在 `/etc/nginx/conf.d/benchlink.conf` 的 HTTPS server 内新增一个 include，指向 `/etc/nginx/codex-usage.location.conf`。原配置备份在项目 `private/nginx-*.conf`。修改前后均运行 `nginx -t`，通过后 reload。

沿用现有 `benchlink-ip` 公信 IP 证书，未关闭证书校验、未修改证书续期服务。部署时证书 SAN 包含该 IP，有效期至 2026-10-29 19:25:55 UTC。已有 `benchlink-cert-renew.timer`，最近一次服务结果 success。证书续期是现有站点的维护依赖。

## 与 DD 草案的实施差异

DD 建议的服务端 .NET 10 在首版改为 Python 3.12 + Starlette + Psycopg，以便在当前低内存、已有 Docker 的主机上实施和验证。客户端 .NET Worker 仍未实现。数据结构、PostgreSQL 事务、稳定身份、累计检查点、四周期与质量语义继续采用 DD。

运行依赖固定在 `server/requirements.txt`；Linux wheel 离线打包，Docker 构建不访问 PyPI。Python 和 PostgreSQL 基础镜像经镜像仓库取得，随后在服务器 `deploy/.env` 固定到不可变 digest。该文件包含自动生成的数据库密码，权限 600，不应打印、提交或复制进文档。

`server/schema.sql` 是版本 001 实际迁移，新增哈希凭据表和版本标记；`docs/.../schema.sql` 仍保留原始设计草案。所有 API 查询从认证凭据绑定 tenant，不接受客户端指定 tenant。当前实现通过应用层约束租户范围，尚未设置数据库 RLS；应用角色仅拥有必要表的读写列权限，不是超级用户、不能创建数据库或角色，不能改写原始累计值。

资源上限：API 192 MiB，PostgreSQL 256 MiB，各限 0.5 CPU；实际空闲内存占用应以监控结果为准。API 单进程，最多 8 个并发数据库操作，每个凭据每分钟 180 次请求。单批最多 500 条、512 KiB，不接受压缩请求。该限额是首版保护措施，不是已验证容量承诺。

## API 与凭据

基地址下实现：

| 方法和路径 | 凭据 |
|---|---|
| POST `/v1/usage/batches` | 设备 write |
| POST `/v1/devices/heartbeat` | 设备 write |
| GET `/v1/streams/{stream_id}/state?epoch=0` | 设备 write，同租户可读取共同基线 |
| GET `/v1/usage/summary?device_id=UUID` | 租户 read，device_id 可选且按流归属过滤 |
| GET `/v1/devices/status` | 租户 read |
| GET `/health/live` | 无 |
| GET `/health/ready` | 无，部署新增数据库就绪检查 |

`server.admin provision --output /private/client.json` 初次建立租户、设备及相互分离的 read/write 凭据，原始凭据只写权限 600 文件，数据库存 SHA256。`identity_key` 独立于上传令牌，供将来客户端跨设备生成匿名流 ID；该密钥不需要发送到查询接口。

服务器初次凭据在 `/opt/codex-usage-monitor/private/client.json`，仅 root 可读。本机已通过 SCP 取得并用 Windows 当前账户 DPAPI 加密为 `.local/client.credentials`；中间明文文件已删除。更换 Windows 账户或机器需重新交付，不应将加密文件当作跨机器可移植配置。

本机 `deploy/Query-Usage.ps1` 解密后仅用 read 凭据查询，不打印令牌。原始 JSON 导出使用 `-AsJson`。Windows 采集与数值上报已启用。

管理工具在服务器本地使用，管理员数据库连接仅通过环境变量传递。生产 API 不公开注册/管理接口。撤销凭据使用 `python -m server.admin revoke --token-hash HEX`；新增同租户设备使用 `add-device --from-credentials ... --output ...`，具体见当前操作步骤。`provision` 创建新租户，不能用于给现有租户增加设备。

## 入库和质量规则

初建流必须包含 seq 0 基线，固定 anchor_id；首个捕获值不计入当日。已接收的数据在同一事务内完成语义比对、派生增量和水位推进，提交后才返回 ACK。相同业务键不同内容返回 409，冲突审计在回滚后另存。

语义哈希序列为 `[schema_version,stream_id,epoch,anchor_id,source_seq,kind,event_time,model,input,cached_input,output,reasoning_output,baseline_reason]`，其中 epoch 为 JSON 整数，source_seq/计数为十进制字符串，snapshot 的 baseline_reason 为 null。使用 UTF8、不转义非 ASCII 字符、不加空白的 JSON 数组，时间统一 UTC 毫秒。observed_at、parser_version、批次及上传设备不参与哈希；服务端自行计算。

缺段只持久保存，不跨段计算。负增量、增量子集不一致、时间逆序或未来超过 5 分钟的记录隔离，暂停该流后续派生；不将负值截断为零。异常不会自动改写历史或自动换 epoch，需要核对源日志后由维护者处理。capture_start 标记 history_not_imported；未验证的继承基线保守标记 inherited_baseline。

四窗口使用同一 REPEATABLE READ 快照、半开区间 `[start,as_of)`。北京时间今天 00:00、周一 00:00、每月 1 日 00:00 分别为自然周期起点。迟到数据按事件时间归属。累计数字和汇总数字均使用十进制字符串传输，避免 JavaScript 精度损失。

设备心跳或扫描时间超过 180 秒、队列未清空、未知/未支持来源、缺段、异常和未导入历史均影响覆盖状态。没有已观察用量时返回 unknown。complete 仅代表已登记、适配器支持且健康来源的覆盖，不能作为计费账单或全账户完整性证明。

## 部署与检查

首次构建前，在 Windows 项目中准备 Linux wheel 并打包：

```powershell
& '.local/venv/Scripts/python.exe' -m pip download --platform manylinux2014_x86_64 --python-version 312 --implementation cp --abi cp312 --only-binary=:all: -r server/requirements-test.txt -d .local/wheels
& '.local/venv/Scripts/python.exe' deploy/package.py
```

上传包不包含 `.local` 凭据、服务器 `.env` 或 `private`。服务器先校验 SHA256，再解压到项目根目录。不要将升级包直接覆盖未备份的配置和凭据。当前操作方式：

```sh
cd /opt/codex-usage-monitor
sh deploy/bootstrap.sh
sh deploy/test-database.sh
python3 deploy/enable-nginx.py
docker compose --env-file deploy/.env -f deploy/compose.yml ps
curl -fsS http://127.0.0.1:8190/health/ready
```

测试脚本使用独立 `codex_usage_test` 数据库，成功/失败后均尝试删除，绝不清理生产数据表。真实接口 smoke 使用临时租户并在 finally 清理合成数据。Windows 当前 HTTP 测试需要普通本机网络权限；受限环境会在 asyncio socketpair 初始化时阻塞，这不是业务测试通过。

## 备份和恢复

已安装系统服务 `codex-usage-backup.service` 和每日 `codex-usage-backup.timer`，按服务器时区每天 04:20 起随机延迟最多 10 分钟，漏跑补执行。`deploy/backup.sh` 以 pg_dump 自定义格式落盘，再保存 SHA256，保留约 7 天。仅删除该项目备份目录中匹配自身命名规则的旧文件。

备份位于服务器 `/opt/codex-usage-monitor/backups`，目录 700、文件 600。首次备份已恢复到独立临时数据库，核对到 1 个租户、2 个凭据、0 条真实/测试用量记录；临时数据库已删除。

恢复时先停止本项目 API，在新的空数据库中执行 pg_restore 并校验，再切换连接。不要直接覆盖正在使用的生产库。还应单独安全保管 `private/client.json`、`.env` 和客户端的匿名化密钥；数据库只存令牌哈希，不能从备份恢复原始令牌。

**目前只有同机备份，没有异地副本，不能覆盖整台服务器或磁盘丢失。** 尚未实施 DD 的 13 个月清理、异地备份和完整 RPO/RTO 演练。

## 回滚与停止

仅停止本项目 API：

```sh
cd /opt/codex-usage-monitor
docker compose --env-file deploy/.env -f deploy/compose.yml stop api
```

撤下公网入口时，从 `/etc/nginx/conf.d/benchlink.conf` 删除本项目新增的 `include /etc/nginx/codex-usage.location.conf;` 一行，然后 `nginx -t` 通过再 reload。保留当前其他配置；不要将旧全量备份覆盖后来其他项目的改动。按需停止本项目备份 timer。不要使用 `docker compose down -v`，否则会删除数据库卷。

## Windows 客户端接入与后续范围

Windows 常规累计检查点适配、SQLite 游标和 Outbox、独立后台、自启动、真实数值上报及原生四周期悬浮窗已于 2026-10-26 实施。本机真实日志和完整上传/查询/显示链路已验证，13 项客户端合成恢复测试通过。客户端采用 Python 标准库与 Windows 自带 .NET Framework WPF，详见 `client/README.md`。原版悬浮窗保持原样，新窗口为独立组件。

压缩补充用量、子代理继承、压缩文件、WSL 与完整历史导入仍待适配。本次采用捕获起点基线，较早的历史不会被全部归入启用当天。生产库已开始接收真实数值，不再是无记录状态。当前服务端 RELEASE.json 的 0 条记录描述的是 2026-10-25 服务端交付时快照，客户端后续状态以 client 交付记录和实际查询为准。

客户端访问继续使用 IP HTTPS。SSH 使用 `-F NUL -o ProxyCommand=none -o ProxyJump=none` 直连；用户已关闭全局 VPN。本次先前添加的临时 `/32` WLAN 路由非持久化，重启后消失。如重新开启全局 VPN，其流量保护仍可能拦截直连，需要由用户配置分流。
