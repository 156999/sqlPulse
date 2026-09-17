#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

has_cmd() { command -v "$1" >/dev/null 2>&1; }

if [[ $EUID -eq 0 ]]; then
  SUDO=()
else
  SUDO=(sudo)
fi

echo "==> [1/4] 检查 Docker 环境"
if ! has_cmd curl; then
  "${SUDO[@]}" apt-get update
  "${SUDO[@]}" apt-get install -y curl
fi

if ! has_cmd docker; then
  echo "==> 未检测到 Docker，开始安装"
  curl -fsSL https://get.docker.com | "${SUDO[@]}" sh
fi

if ! docker compose version >/dev/null 2>&1 && ! has_cmd docker-compose; then
  echo "==> 安装 Docker Compose 插件"
  "${SUDO[@]}" apt-get update
  "${SUDO[@]}" apt-get install -y docker-compose-plugin
fi

if command -v systemctl >/dev/null 2>&1; then
  "${SUDO[@]}" systemctl enable --now docker || true
fi

RUN=()
if [[ $EUID -ne 0 ]] && ! docker info >/dev/null 2>&1; then
  RUN=(sudo)
fi

echo "==> [2/4] 准备 .env"
if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "    已从 .env.example 创建 .env"
fi

if grep -Eq '^APP_SECRET_KEY=[[:space:]]*$' .env; then
  if has_cmd python3; then
    KEY="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
  else
    KEY="$(openssl rand -hex 32)"
  fi
  sed -i "s|^APP_SECRET_KEY=.*|APP_SECRET_KEY=${KEY}|" .env
  echo "    已自动生成 APP_SECRET_KEY"
fi
sed -i 's/\r$//' .env

if docker compose version >/dev/null 2>&1; then
  DC=(docker compose)
else
  DC=(docker-compose)
fi

echo "==> [3/4] 构建并启动容器"
"${RUN[@]}" "${DC[@]}" up -d --build

echo "==> [4/4] 等待服务就绪"
READY=0
for _ in $(seq 1 60); do
  if "${RUN[@]}" "${DC[@]}" exec -T web python -c \
      "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=2)" \
      >/dev/null 2>&1; then
    READY=1
    break
  fi
  sleep 2
done

WEB_PORT="$(grep -E '^WEB_PORT=' .env | cut -d= -f2- || echo 8080)"
if [[ -z "$WEB_PORT" ]]; then
  WEB_PORT=8080
fi

echo "==> 部署完成"
if [[ $READY -eq 1 ]]; then
  echo "    访问: http://<服务器公网IP>:${WEB_PORT}"
else
  echo "    容器已启动但健康检查尚未通过，请稍后查看: ${DC[*]:-} ps / ${DC[*]:-} logs -f web"
fi
echo "    默认账号: root / root"
echo "    数据落盘: docker 命名卷（docker compose down 不丢数据，加 -v 才会清空）"
