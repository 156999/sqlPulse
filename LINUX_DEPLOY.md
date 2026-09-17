# SQL Pulse Linux（Ubuntu）Docker 部署

本文档适用于腾讯云 Ubuntu 服务器，也可以直接用于其他 Ubuntu / Debian 系服务器。部署完成后，SQL Pulse 网页、示例 MySQL、数据目录全部由 Docker 托管。

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
4. 自动生成并写入 `APP_SECRET_KEY`
5. 执行 `docker compose up -d --build`
6. 等待 web 容器健康检查通过

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

注意：示例 MySQL 在宿主机上映射为 `3307`，只用于本机/内网访问，**不要**对公网放行 `3307`。

## 5. 容器与挂载说明

项目代码不挂载到宿主机，代码在构建时 `COPY` 进镜像；挂载只涉及数据持久化。

web 容器：

```text
sqlpulse_data    -> /app/data
sqlpulse_logs    -> /app/logs
sqlpulse_reports -> /app/reports
```

mysql 容器：

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
docker volume inspect sqlpulse_mysql_data
```

`docker compose down` 不会删除数据；`docker compose down -v` 才会清空卷并重新初始化示例库。

## 6. 上线前安全配置

编辑 `/opt/sqlpulse/.env`：

```ini
MYSQL_ROOT_PASSWORD=换成强密码
AUTH_ALLOW_REGISTER=false
AUTH_COOKIE_SECURE=true
```

说明：

- `APP_SECRET_KEY` 已由 `deploy.sh` 自动生成，不需要手工填写
- `AUTH_ALLOW_REGISTER=false` 关闭开放注册
- `AUTH_COOKIE_SECURE=true` 仅在 HTTPS 访问时使用；纯 HTTP 场景保持 `false`
- 端口冲突时可改 `WEB_PORT` 和 `MYSQL_HOST_PORT`

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

# 查看 MySQL 日志
docker compose logs -f mysql

# 更新代码后重建
bash deploy.sh

# 停止服务（数据保留）
docker compose down

# 停止并清空全部数据（慎用）
docker compose down -v
```

## 8. 常见问题

- 网页打不开：先确认安全组放行 `8080`，再执行 `docker compose ps`，确认 `web` 和 `mysql` 都是 `healthy`。
- 端口被占用：修改 `.env` 中 `WEB_PORT` / `MYSQL_HOST_PORT`，再执行 `bash deploy.sh`。
- 示例库没有初始化：首次初始化只执行一次；需要重新初始化时执行 `docker compose down -v && bash deploy.sh`。
- 想要 HTTPS：用 Nginx / Caddy 反代本机 `8080`，并将 `AUTH_COOKIE_SECURE=true`。
- 压测目标库：compose 内部默认使用内置 `mysql:3306`；本地 Python 模式才读取 `.env` 中 `TARGET_DB_*`。
