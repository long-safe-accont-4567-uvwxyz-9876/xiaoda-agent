## Docker 常用命令

### 容器管理
- `docker ps` - 查看运行中的容器
- `docker ps -a` - 查看所有容器
- `docker run -d --name xxx image` - 后台运行容器
- `docker stop/start/restart xxx` - 容器生命周期
- `docker logs -f xxx` - 查看日志
- `docker exec -it xxx bash` - 进入容器
- `docker rm xxx` - 删除容器
- `docker rm -f xxx` - 强制删除运行中的容器

### 镜像管理
- `docker images` - 查看镜像
- `docker pull image:tag` - 拉取镜像
- `docker build -t name:tag .` - 构建镜像
- `docker push repo/image:tag` - 推送镜像
- `docker rmi image` - 删除镜像
- `docker image prune` - 清理无用镜像

### 端口和卷
- `docker run -p 8080:80 image` - 端口映射
- `docker run -v /host:/container image` - 卷挂载
- `docker volume ls` - 查看卷
- `docker volume create name` - 创建卷
- `docker volume rm name` - 删除卷

### 网络管理
- `docker network ls` - 查看网络
- `docker network create name` - 创建网络
- `docker network connect net container` - 连接网络
- `docker network inspect net` - 查看网络详情

### Docker Compose
- `docker-compose up -d` - 启动服务
- `docker-compose down` - 停止服务
- `docker-compose logs -f` - 查看日志
- `docker-compose ps` - 查看状态
- `docker-compose exec service bash` - 进入服务

### 系统管理
- `docker system df` - 查看磁盘占用
- `docker system prune` - 清理无用资源
- `docker system prune -a` - 清理所有无用镜像
- `docker info` - 查看 Docker 信息

### 常用组合
- 开发容器: `docker run -it --rm -v $(pwd):/app -w /app -p 3000:3000 node:18 npm run dev`
- 数据库容器: `docker run -d --name postgres -e POSTGRES_PASSWORD=secret -v postgres-data:/var/lib/postgresql/data -p 5432:5432 postgres:15`
- 调试容器: `docker exec -it container sh`
- 复制文件: `docker cp container:/path/file ./local`
