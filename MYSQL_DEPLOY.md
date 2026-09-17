# SQL Pulse MySQL Docker 部署与外部连接

本文档说明如何在 Linux / Ubuntu 服务器上用 Docker 部署 MySQL，并允许外部客户端（本机、Navicat、DBeaver 等）连接。

## 1. 两种方案

- 方案一：使用 SQL Pulse 已有的 `docker-compose.yml` 内置 MySQL
- 方案二：单独启动一个 MySQL 容器，与 SQL Pulse 解耦

如果只是给 SQL Pulse 用，选方案一；如果需要独立的公共 MySQL，选方案二。

## 2. 方案一：SQL Pulse 内置 MySQL

进入项目目录并设置宿主机映射端口：

```bash
cd /opt/sqlpulse
cp .env.example .env

# 外部连接端口设为 3306；如果服务器已有 3306，可以改成 3307
sed -i 's/^MYSQL_HOST_PORT=.*/MYSQL_HOST_PORT=3306/' .env

bash deploy.sh
```

查看容器状态：

```bash
cd /opt/sqlpulse
docker compose ps
```

创建外部连接用户：

```bash
docker compose exec mysql mysql -uroot -p
```

进入 MySQL 后执行：

```sql
CREATE USER 'remote'@'%' IDENTIFIED BY 'RemoteStrongPass';
GRANT ALL PRIVILEGES ON *.* TO 'remote'@'%' WITH GRANT OPTION;
FLUSH PRIVILEGES;
```

如果只想给 SQL Pulse 示例库权限：

```sql
GRANT ALL PRIVILEGES ON sqlpulse_demo.* TO 'remote'@'%';
FLUSH PRIVILEGES;
```

说明：

- 容器内部始终是 `3306`
- 宿主机外部端口由 `.env` 的 `MYSQL_HOST_PORT` 控制
- MySQL 容器名通常为 `sqlpulse-mysql-1`，但推荐始终用 `docker compose exec mysql` 操作

## 3. 方案二：独立 MySQL 容器

```bash
docker run -d \
  --name mysql-public \
  --restart unless-stopped \
  -p 3306:3306 \
  -e MYSQL_ROOT_PASSWORD='RootStrongPass' \
  -e MYSQL_DATABASE='app_db' \
  -e MYSQL_USER='app_user' \
  -e MYSQL_PASSWORD='AppUserPass' \
  -v mysql_public_data:/var/lib/mysql \
  mysql:8.0
```

参数说明：

- `-p 3306:3306`：宿主机外部端口 `3306` 映射到容器 `3306`
- `MYSQL_ROOT_PASSWORD`：root 密码
- `MYSQL_DATABASE`：自动创建的数据库
- `MYSQL_USER` / `MYSQL_PASSWORD`：自动创建的普通用户
- `mysql_public_data`：数据卷，容器删除后数据仍保留

创建专用外部用户：

```bash
docker exec -it mysql-public mysql -uroot -p
```

```sql
CREATE USER 'remote'@'%' IDENTIFIED BY 'RemoteStrongPass';
GRANT ALL PRIVILEGES ON app_db.* TO 'remote'@'%';
FLUSH PRIVILEGES;
```

给所有库权限：

```sql
GRANT ALL PRIVILEGES ON *.* TO 'remote'@'%' WITH GRANT OPTION;
FLUSH PRIVILEGES;
```

## 4. 腾讯云安全组

在腾讯云控制台给服务器安全组增加入站规则：

- 协议：TCP
- 端口：`3306`
- 来源：你的固定公网 IP，或临时放行时使用 `0.0.0.0/0`

如果方案一使用了 `3307`，端口改为 `3307`。

如果服务器开了 `ufw`：

```bash
sudo ufw allow 3306/tcp
```

建议：

- 不要长期使用 `0.0.0.0/0`
- 尽量只放行固定公网 IP
- 服务器本机改密码不要用简单密码

## 5. 外部客户端连接

命令行测试：

```bash
mysql -h <服务器公网IP> -P 3306 -u remote -p
```

如果端口是 `3307`：

```bash
mysql -h <服务器公网IP> -P 3307 -u remote -p
```

Navicat / DBeaver 填写：

```text
主机：<服务器公网IP>
端口：3306 或 3307
用户名：remote
密码：RemoteStrongPass
```

## 6. 日常管理命令

方案一：

```bash
cd /opt/sqlpulse
docker compose ps
docker compose logs -f mysql
docker compose exec mysql mysql -uroot -p
```

方案二：

```bash
docker ps | grep mysql-public
docker logs -f mysql-public
docker exec -it mysql-public mysql -uroot -p
docker restart mysql-public
```

查看数据卷：

```bash
docker volume ls
docker volume inspect mysql_public_data
```

## 7. 安全建议

- 修改默认 `MYSQL_ROOT_PASSWORD`，不要使用 `s3cret`
- 使用专用账号连接，避免直接暴露 root
- MySQL 直接暴露公网有风险，建议只放行固定 IP
- 长期方案可改为内网 + VPN，或通过 SSH 隧道访问
- 数据卷不要随意执行 `docker compose down -v` / `docker volume rm`

## 8. 常见问题

- 外部连接超时：检查腾讯云安全组、`ufw`、云防火墙
- root 无法外部登录：使用 `'remote'@'%'` 专用账号，或用 `MYSQL_ROOT_HOST=%`
- 端口冲突：服务器已有 3306 时改 `MYSQL_HOST_PORT=3307` 或 `-p 3307:3306`
- 连接被拒绝：执行 `docker compose ps` 或 `docker ps` 确认 MySQL 已启动
- 客户端不支持 `caching_sha2_password`：升级客户端，或对旧客户端创建 `mysql_native_password` 账号
