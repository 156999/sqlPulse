# SQL Pulse Linux（Ubuntu）Docker 部署

本文档适用于腾讯云 Ubuntu 服务器，也可以直接用于其他 Ubuntu / Debian 系服务器。部署完成后，SQL Pulse 网页与数据目录由 Docker 托管。

**被测 MySQL 不随产品部署**：`docker-compose.yml` 只包含 `web` 一个服务，库地址（host / port / 账号 / 密码）在页面「连接管理」里填写，可以指向任意可达的 MySQL 实例。如果你手头暂时没有库，只想先跑起来看一眼，见第 8 节的「可选：叠加示例库」。

## 1. 环境要求

- Ubuntu 20.04 / 22.04 / 24.04
- 建议 2 核 4G 起
- 服务器可以访问外网（安装 Docker、拉取镜像）
- 当前用户可用 `sudo`

## 2. 把项目放到服务器

推荐目录 `/opt/sqlpulse`：

```bash
sudo mkdir -p /opt/sqlpulse
sudo chown $USER /opt/sqlpulse
```

然后二选一：

```bash
# 方式一：Git 拉取
git clone <你的仓库地址> /opt/sqlpulse

# 方式二：从本地 Windows 上传（在本地 PowerShell 执行）
scp -r D:\sqlpulse ubuntu@<服务器公网IP>:/opt/sqlpulse
```

## 3. 一键部署

```bash
cd /opt/sqlpulse
bash deploy.sh
```

`deploy.sh` 会自动完成：

1. 安装 `curl`（如缺失）
2. 安装 Docker 和 Docker Compose 插件（如缺失）
3. 生成 `.env`
4. 自动生成并写入 `APP_SECRET_KEY` 和 `SQLPULSE_CREDENTIAL_KEY`
5. 执行 `docker compose up -d --build`（只启动 `web` 一个服务）
6. 等待 web 容器健康检查通过

⚠️ `SQLPULSE_CREDENTIAL_KEY` 用来加密连接密码，**生成后不要修改**：一旦变更，数据库里已保存的密码将永久无法解密。请把 `.env` 备份到安全位置。

首次构建需要几分钟；之后启动会快很多。

## 4. 安全组与访问

在腾讯云控制台为服务器安全组添加入站规则：

- 协议：TCP
- 端口：`8080`
- 来源：`0.0.0.0/0` 或你常用的公网 IP

然后访问：

```text
http://<服务器公网IP>:8080
```

首次登录账号：

```text
root / root
```

注意：产品部署不含 MySQL。若你叠加了示例库（见第 8 节），它会占用宿主机 `3307` 端口，只用于本机/内网访问，**不要**对公网放行 `3307`。

## 5. 容器与挂载说明

项目代码不挂载到宿主机，代码在构建时 `COPY` 进镜像；挂载只涉及数据持久化。

web 容器：

```text
sqlpulse_data    -> /app/data
sqlpulse_logs    -> /app/logs
sqlpulse_reports -> /app/reports
```

mysql 容器（**仅在叠加 `docker-compose.mysql.yml` 时存在**，产品部署没有这个容器）：

```text
mysql_data                          -> /var/lib/mysql
./scripts/seed_demo.sql (只读 bind) -> /docker-entrypoint-initdb.d/seed.sql
```

命名卷由 Docker 管理，实际位于服务器：

```text
/var/lib/docker/volumes/
```

查看卷：

```bash
docker volume ls
docker volume inspect sqlpulse_sqlpulse_data   # web 的控制面数据：连接配置、任务、报告
docker volume inspect sqlpulse_mysql_data      # 仅叠加示例库时存在
```

`docker compose down` 不会删除数据；`docker compose down -v` 才会清空卷。叠加了示例库时，`-v` 会连示例库一起清空，下次启动重新执行 `seed_demo.sql` 初始化。

## 6. 上线前安全配置

编辑 `/opt/sqlpulse/.env`：

```ini
# 仅当你叠加了示例库（第 8 节）时才需要设置
MYSQL_ROOT_PASSWORD=换成强密码
AUTH_ALLOW_REGISTER=false
AUTH_COOKIE_SECURE=true
```

说明：

- `APP_SECRET_KEY` 与 `SQLPULSE_CREDENTIAL_KEY` 已由 `deploy.sh` 自动生成，不需要手工填写，但**必须备份**：前者失效会导致登录态全废，后者失效会导致已保存的数据库密码永久解不开
- `AUTH_ALLOW_REGISTER=false` 关闭开放注册
- `AUTH_COOKIE_SECURE=true` 仅在 HTTPS 访问时使用；纯 HTTP 场景保持 `false`
- 端口冲突时可改 `WEB_PORT`；`MYSQL_HOST_PORT` 仅在叠加示例库时生效

修改后重新执行：

```bash
bash deploy.sh
```

## 7. 日常管理

```bash
cd /opt/sqlpulse

# 查看状态
docker compose ps

# 查看 web 日志
docker compose logs -f web

# 更新代码后重建
bash deploy.sh

# 停止服务（数据保留）
docker compose down

# 停止并清空全部数据（慎用）
docker compose down -v
```

叠加了示例库时，必须带上 `-f`，否则 compose 根本不知道有 `mysql` 这个服务：

```bash
DC="docker compose -f docker-compose.yml -f docker-compose.mysql.yml"

$DC ps
$DC logs -f mysql
$DC down            # 加 -v 会连示例库数据一起清空
```

## 8. 可选：叠加示例库（演示 / 试用）

产品部署不含 MySQL，被测库在页面里填。如果手上暂时没有可用的库，只想先把系统跑起来看效果，可以叠加仓库自带的示例库夹具：

```bash
cd /opt/sqlpulse
docker compose -f docker-compose.yml -f docker-compose.mysql.yml up -d --build
```

它会额外启动一个 `mysql:8.0` 容器并自动导入 `scripts/seed_demo.sql`：

| 表 | 行数 | 说明 |
|---|---|---|
| `orders` | 10000 | 主表，`id` 有主键索引 |
| `stock` | 500 | 库存表 |
| `access_log` | 50000 | `path` 无索引，用来演示慢 SQL 被规则引擎抓到 |

宿主机端口映射为 `3307`，容器内仍是 `3306`。启动后「新建连接」表单的初始值已被预填为这个库，点一下「测试连通性」即可通过，不用自己猜地址。

重灌数据（脚本自带 `DROP TABLE IF EXISTS`，可反复执行）：

```bash
make seed
```

两个前提：

- 服务名必须叫 `mysql` —— `tests/integration` 的用例按服务名解析 DSN。
- 种子脚本只在 `mysql_data` 卷为空时自动执行一次。想重新初始化：先 `down -v` 再起。

压测前建议刷新统计信息，否则 EXPLAIN 页的执行计划会失真：

```bash
docker compose -f docker-compose.yml -f docker-compose.mysql.yml \
  exec mysql sh -c 'env MYSQL_PWD="$MYSQL_ROOT_PASSWORD" mysql -uroot sqlpulse_demo \
  -e "ANALYZE TABLE orders, access_log, stock;"'
```

## 9. 常见问题

- 网页打不开：先确认安全组放行 `8080`，再执行 `docker compose ps`，确认 `web` 是 `healthy`；叠加了示例库时再看 `mysql` 是否也 `healthy`。
- web 构建时 `pip install` 报 `files.pythonhosted.org ... Read timed out`：这是 Docker 构建阶段拉取 Python 依赖超时。确认 `.env` 中有 `PIP_INDEX_URL=https://mirrors.cloud.tencent.com/pypi/simple`，如仍超时可改为 `https://pypi.tuna.tsinghua.edu.cn/simple`，然后重新执行 `bash deploy.sh`。
- Docker 拉取镜像报错 `docker.mirrors.ustc.edu.cn ... no such host`：这是 Docker daemon 配置了失效镜像源，不是项目配置问题。先查看当前镜像源：

  ```bash
  docker info --format '{{json .RegistryConfig.Mirrors}}'
  sudo cat /etc/docker/daemon.json
  ```

  然后改为腾讯云镜像源并重启 Docker：

  ```bash
  sudo cp /etc/docker/daemon.json /etc/docker/daemon.json.bak.$(date +%Y%m%d%H%M%S)
  sudo tee /etc/docker/daemon.json >/dev/null <<'EOF'
  {
    "registry-mirrors": ["https://mirror.ccs.tencentyun.com"]
  }
  EOF
  sudo systemctl daemon-reload
  sudo systemctl restart docker
  ```

  回到 `/opt/sqlpulse` 重新执行 `bash deploy.sh`。如果该镜像源仍不可用，可临时改用 `https://docker.m.daocloud.io`，或移除 `registry-mirrors` 后重启 Docker。
- 端口被占用：修改 `.env` 中 `WEB_PORT`（叠加示例库时还有 `MYSQL_HOST_PORT`），再执行 `bash deploy.sh`。
- 示例库没有初始化：种子脚本只在 `mysql_data` 卷为空时执行一次。先确认起服务时带了 `-f docker-compose.mysql.yml`；需要重新初始化时执行 `docker compose -f docker-compose.yml -f docker-compose.mysql.yml down -v`，再重新启动。
- 想要 HTTPS：用 Nginx / Caddy 反代本机 `8080`，并将 `AUTH_COOKIE_SECURE=true`。
- 压测目标库：在页面「连接管理」里填 host / port / 账号 / 密码，指向任意可达的 MySQL。`.env` 中的 `TARGET_DB_*` 只是给表单**预填**初值，启动时不会去连库，填错也不影响启动。⚠️ 容器内访问宿主机上的库不能用 `127.0.0.1`（那指向容器自己）：Windows / macOS 填 `host.docker.internal`；Linux 最省事是直接填宿主机内网 IP，或在 compose 里给 `web` 补 `extra_hosts: ["host.docker.internal:host-gateway"]`。
