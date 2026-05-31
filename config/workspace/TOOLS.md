# TOOLS.md - 工具使用规则

## 工具使用原则

- 优先使用只读工具理解项目
- 修改文件前必须先确认文件内容和上下文
- 运行命令时优先使用项目已有脚本
- 不运行来源不明的危险命令
- 不输出密钥、Token、Cookie 或私人凭证
- 不在未确认时删除文件、清空目录或覆盖重要数据

## 文件操作工具

### 可直接使用

- list_files：列出目录内容
- read_file：读取文件
- search_files：搜索文件

### 需要谨慎

- write_file：写入文件前先确认目标路径和内容
- shell_command：评估命令风险

## 代码工具

### 可直接使用

- python_executor：执行 Python 代码
- calculator：数学计算

### 注意事项

- Python 代码执行前确认不会有副作用
- 不执行删除文件、格式化磁盘等危险代码

## 网络工具

### 可直接使用

- web_search：搜索互联网
- get_weather：获取天气
- multi_search：多引擎搜索（国内：Bing/Baidu/Sogou/360，国际：DuckDuckGo）
- wolfram_query：WolframAlpha知识计算（数学/单位转换/科学查询）

### 注意事项

- 不访问恶意网站
- 不下载未经验证的可执行文件
- multi_search 自动根据查询语言选择国内或国际引擎

## Docker 常用命令

### 容器管理
- `docker ps` - 查看运行中的容器
- `docker ps -a` - 查看所有容器
- `docker run -d --name xxx image` - 后台运行容器
- `docker stop/start/restart xxx` - 容器生命周期
- `docker logs -f xxx` - 查看日志
- `docker exec -it xxx bash` - 进入容器

### 镜像管理
- `docker images` - 查看镜像
- `docker pull image` - 拉取镜像
- `docker build -t name:tag .` - 构建镜像
- `docker rmi image` - 删除镜像

### 系统管理
- `docker system df` - 查看磁盘占用
- `docker system prune` - 清理无用资源

### Docker Compose
- `docker-compose up -d` - 启动服务
- `docker-compose down` - 停止服务
- `docker-compose logs -f service` - 查看日志

## Shell 命令策略

### 可以直接运行

```bash
# 查看文件和目录
ls, cat, head, tail, find, grep, tree

# Python 相关
python --version, pip list, conda list

# 项目测试和检查
npm test, npm run lint, python -m pytest

# 系统信息
uname -a, df -h, free -m
```

### 需要谨慎确认

```bash
# 文件删除
rm, rm -rf, rmdir

# 配置覆盖
mv, cp -f

# 系统服务
systemctl stop/restart, kill, killall

# 权限修改
chmod, chown

# 依赖安装
pip install (大批量), apt install
```

## 审批策略

- 低风险操作：直接执行
- 中风险操作：说明风险后执行
- 高风险操作：必须等旅行者确认后执行
