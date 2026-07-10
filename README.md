# Minecraft Seed AI Finder

面向 Minecraft Java 的自然语言种子搜索工具。输入“村庄背靠樱花林、坐落于草原、面朝大海，并且 800 格内有女巫小屋”这类条件，系统会拆解结构、群系、距离和方向约束，再返回坐标、地图与检查结果。

项目由 FastAPI 后端、Minecraft native 查询核心和 Minecraft 风格前端组成。默认按 Minecraft Java 26.2 的兼容规则工作，并明确标注近似结果。

## 一键运行

Docker Hub 镜像：

```text
lx969788249/mc-seed-ai-finder:latest
```

宿主机只需要 Docker。首次启动会自动生成 Fernet 加密密钥并保存到数据卷：

```bash
docker run -d \
  --name mc-seed-ai-finder \
  --restart unless-stopped \
  -p 8000:8000 \
  -v mc-seed-data:/app/data \
  lx969788249/mc-seed-ai-finder:latest
```

打开 `http://服务器IP:8000/` 即可使用。数据卷 `mc-seed-data` 保存 SQLite 数据、自我进化报告和自动生成的密钥。不要删除这个卷，否则已保存的 DeepSeek API Key 将无法解密。

查看日志：

```bash
docker logs -f mc-seed-ai-finder
```

当前镜像面向 `linux/amd64`；ARM 服务器需要自行构建对应架构镜像。

## 使用流程

1. 注册或登录账号。
2. 在设置中填写 DeepSeek API Key、种子、版本和起点坐标。
3. 输入自然语言条件，例如：

   ```text
   找一个村庄，背靠樱花林，坐落于草原，面朝大海，附近 800 格内有女巫小屋
   ```

4. 查看候选坐标、约束检查、地图和 Chunkbase 核对链接。

未配置 API Key 时，系统可以展示目录和地图能力，但不会执行需要 DeepSeek 解析的搜索。

## 支持的能力

- 结构搜索：村庄、女巫小屋、掠夺者前哨站、神殿、海底神殿、沉船、远古城市等。
- 群系搜索：平原、樱花林、森林、蘑菇岛、海洋、深海、沙滩等。
- 组合条件：邻近、距离上限、同一群系、背靠/面朝、相对方向和排除条件。
- 面积条件：估算群系连通面积，支持“最大的蘑菇岛”和“靠近更大的海洋”等描述。
- 地表俯视地图：用近似地形高度生成山脊、坡面和海岸明暗，避免把洞穴群系显示成地表群系。
- 多核 native 查询：独立查询通过多个 subprocess 并行执行，并支持 batch 和锚点组合剪枝。
- 未满足需求反馈：用户可以标记结果未解决，后台会脱敏记录并在夜间聚类。

## 精度边界

当前后端使用 cubiomes 查询核心，并将 Java 26.2 映射到兼容规则。世界生成规则或结构布局变化时，结果可能与实际版本不同，响应会返回兼容模式和相关说明。

地表高度、群系面积和全域最大值都是可审计的近似计算：

- 地图高度是采样后的近似地表高度，不是完整方块高度图。
- 群系面积按采样网格和连通区域估算，排名靠前的候选会做更细采样。
- “最大”表示当前搜索覆盖范围内的近似最大值，不宣称完成全世界穷举。

## Docker Compose

需要从源码构建或修改配置时：

```bash
git clone --recurse-submodules https://github.com/lx969788249/mc-seed-ai-finder.git
cd mc-seed-ai-finder
docker-compose up -d --build
```

新版 Docker Compose 也可以使用 `docker compose up -d --build`。默认端口为 `8000`，可以在 `.env` 中设置：

```env
MCFINDER_PORT=8080
MC_QUERY_WORKERS=8
MC_QUERY_BATCH_SIZE=64
```

## 本地开发

依赖：Python 3.11+、C 编译器、`make`、Git 和 cubiomes 子模块。

```bash
git clone --recurse-submodules https://github.com/lx969788249/mc-seed-ai-finder.git
cd mc-seed-ai-finder
git submodule update --init --recursive
make native
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

如果 Debian/Ubuntu 缺少 `ensurepip`，先执行 `sudo apt-get install -y python3.11-venv`。

启动开发服务器：

```bash
set -a
[ -f .env ] && . ./.env
set +a
uvicorn backend.main:app --host 0.0.0.0 --port 8000
```

## 配置项

| 变量 | 作用 | 默认值 |
| --- | --- | --- |
| `APP_ENCRYPTION_KEY` | 加密数据库中的 DeepSeek API Key；Docker 未指定时自动生成 | 自动生成 |
| `MCFINDER_DB` | SQLite 数据库路径 | `data/app.sqlite3` |
| `MCFINDER_EVOLUTION_DIR` | 自我进化报告目录 | `data/evolution` |
| `MC_QUERY_WORKERS` | native 查询并行度 | 根据 CPU 自动选择 |
| `MC_QUERY_BATCH_SIZE` | 每个 native batch 的查询数量 | `64` |
| `EVOLUTION_NIGHT_HOUR` | 夜间聚合小时 | `3` |
| `EVOLUTION_NIGHT_MINUTE` | 夜间聚合分钟 | `30` |
| `EVOLUTION_RETENTION_DAYS` | 反馈保留天数 | `90` |

## 自我进化闭环

系统会记录明确不支持、没有可验证结果、约束未满足、执行异常和用户主动标记未解决的事件。记录会脱敏，不保存 API Key、Session Token、密码、Cookie 或 IP。

每天本地时间 `03:30`，后台等待搜索任务归零后进行去重聚类和优先级排序，报告写入：

- `data/evolution/latest.json`：供后续编码代理消费的任务清单。
- `data/evolution/latest.md`：人工审阅报告。
- `data/evolution/runs/`：历史聚合结果。

当前阶段只生成修复任务，不会让线上进程直接修改或发布自身代码。后续接入 Codex 时，应保留隔离分支、自动测试、行为验收和人工批准四道门槛。

手动生成报告：

```bash
python3 -m backend.evolution
```

## API 入口

- `POST /auth/register`
- `POST /auth/login`
- `POST /auth/logout`
- `GET /me`
- `GET /settings`
- `PUT /settings`
- `POST /chat`
- `POST /search`
- `POST /feedback/unmet`
- `GET /evolution/status`
- `GET /catalog`
- `GET /backends`

`/chat` 使用登录用户保存的配置；`/search` 支持直接传入 `query`、`seed`、`version`、`center_x`、`center_z` 和 `max_results` 做无登录测试。

## 数据和安全

- API Key 只在后端保存，使用 Fernet 加密。
- 登录密码使用 PBKDF2-HMAC-SHA256 哈希保存。
- 数据库和进化报告默认位于 `data/`，Docker 中位于 `/app/data`。
- `.env`、SQLite 文件、日志、崩溃转储和编译产物不会提交到 Git。
- 生产环境应使用命名卷或宿主机备份，并限制 Docker 容器访问权限。

## 验收测试

```bash
make test
curl -fsS http://127.0.0.1:8000/catalog
```

当前测试覆盖面积搜索、相对布局、地表地图和自我进化反馈，共 18 项标准库 `unittest` 测试。

## 许可证和依赖

项目使用 cubiomes 作为 Git 子模块，许可证文件位于 `vendor/cubiomes/LICENSE`。重新分发镜像或源码时请一并保留上游许可证。
