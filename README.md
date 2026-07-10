# Minecraft Java 种子地点 AI 查找器 MVP

这是一个可运行的 Linux MVP：注册/登录、保存 DeepSeek API Key 与种子设置、聊天式查询、后端规划搜索、前端展示结构化坐标结果。

## 当前搜索精度

内置 `deterministic_compat` 搜索核心是确定性兼容模式，用于打通端到端工作流，不宣称 Minecraft Java 26.2 精确世界生成。接口会在结果里返回 `mode: compatibility`。

项目把 cubiomes 固定为 Git 子模块，并通过根目录 `Makefile` 构建 native 查询核心：

```bash
git submodule update --init --recursive
make native
```

## 安装与启动

```bash
cd /root/mc-seed-ai-finder
git submodule update --init --recursive
make native
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

如果 Debian/Ubuntu 提示缺少 `ensurepip`，先安装 venv 支持：

```bash
sudo apt-get install -y python3.11-venv
```

本机临时测试也可以不用 venv：

```bash
python3 -m pip install --user --break-system-packages -r requirements.txt
```

把生成的值写入 `.env` 的 `APP_ENCRYPTION_KEY`。开发测试也可以不写，此时 API Key 仍会用开发密钥 Fernet 加密保存，但不适合生产。

启动：

```bash
source .venv/bin/activate
set -a; [ -f .env ] && . ./.env; set +a
uvicorn backend.main:app --host 0.0.0.0 --port 8000
```

浏览器打开 `http://服务器IP:8000/`。

## 性能配置

搜索核心会把独立的 cubiomes/native 查询拆成多个 subprocess 并行运行。默认并行度是 `min(8, CPU核心数)`。

### 地表俯视地图

地图 tile 会先用 cubiomes 估算每个采样点的地表高度，再在地表上方读取群系。前端根据相邻高度生成地形明暗，因此山脊、坡面和海岸会以俯视地形显示，不再把固定 `Y=63` 处的繁茂洞穴、溶洞或深暗之域画到山体表面。悬停信息中的地表高度属于近似值，不代表完整的方块级高度图。

### 群系面积搜索

搜索计划支持 `biome_area_constraints` 和 `largest_area`：

- “找这个种子里最大的蘑菇岛”会在可玩范围内执行确定性抽样，粗测候选后对排名靠前的区域做 4 格精测。
- “找靠近大海的村庄，大海要大一点”会把模糊大小转换为海洋连通面积下限，并参与候选审核。
- 面积按 `Y=63` 的二维群系投影计算；全域最大值属于高覆盖近似排名，不宣称穷举世界后得到数学意义上的绝对最大值。

可在 `.env` 中调整：

```bash
MC_QUERY_WORKERS=8
MC_QUERY_BATCH_SIZE=64
```

`MC_QUERY_WORKERS` 控制同时运行的 native 查询进程数；`MC_QUERY_BATCH_SIZE` 控制每个 batch subprocess 内包含多少个局部查询。严苛组合搜索、大范围 tile 扫描和锚点附近目标筛选都会使用这个并行配置。

强锚点组合搜索会优先使用 `native/mc_query anchor_combo`，把“一个锚点附近依次检查多个目标”的剪枝流程放到 C/native 进程里执行；如果 native 组合接口不可用，后端会回退到逐目标批量查询。

## 自我进化闭环

后端会自动记录明确不支持、没有可验证结果、约束未满足和执行异常的搜索。用户也可以在结果区点击“标记未解决”，该反馈会以更高权重进入改进队列。记录只包含脱敏后的请求、复现所需世界参数和搜索计划，不保存 API Key、Session Token、密码、Cookie 或 IP。

每天本地时间 `03:30`，调度器会等待当前搜索任务归零，再对最近 90 天事件做去重聚类和优先级排序。报告写入：

- `data/evolution/latest.json`：供后续编码代理消费的任务清单。
- `data/evolution/latest.md`：便于人工审阅的优先级报告。
- `data/evolution/runs/`：每次夜间聚合的历史快照。

可以手动生成一次报告：

```bash
python3 -m backend.evolution
```

可通过环境变量调整：

```bash
EVOLUTION_NIGHT_HOUR=3
EVOLUTION_NIGHT_MINUTE=30
EVOLUTION_RETENTION_DAYS=90
EVOLUTION_MAX_EVENTS_PER_DAY=5000
EVOLUTION_MAX_CLUSTER_EVENTS_PER_DAY=100
```

当前阶段只自动生成修复任务，不允许线上进程直接修改或发布自身代码。自动修复需要在后续接入隔离分支、自动测试、行为验收和人工批准四道门槛后启用。

## API

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

`/chat` 使用登录用户保存的配置。`/search` 可直接传 `query/seed/version/center_x/center_z/max_results` 做无登录测试。搜索不会因为用户配置的半径提前停止，会自动扩大到 Minecraft Java 可玩世界范围；用户自然语言里的“附近 N 格内”只作为目标之间的距离约束。

## 安全说明

- DeepSeek API Key 不写入前端源码。
- 后端不会打印 API Key。
- 登录密码使用 PBKDF2-HMAC-SHA256 hash 保存。
- API Key 使用 `APP_ENCRYPTION_KEY` 派生的 Fernet 密钥加密保存。
- SQLite 数据库默认在 `data/app.sqlite3`。

## 验收测试

```bash
curl -s http://127.0.0.1:8000/catalog
curl -s -X POST http://127.0.0.1:8000/search \
  -H 'Content-Type: application/json' \
  -d "{\"query\":\"帮我找一下离我最近的村庄\",\"seed\":\"12345\",\"version\":\"26.2\",\"center_x\":0,\"center_z\":0,\"max_results\":1,\"deepseek_api_key\":\"${DEEPSEEK_API_KEY}\"}"
```

网页流程：

1. 注册并自动登录。
2. 保存 DeepSeek API Key、seed、版本和当前位置。
3. 输入“帮我找一下离我最近的村庄”。
4. 查看 AI 回复、候选坐标、约束结果和 Chunkbase 核对链接。
5. 不填 API Key 时，系统会提示先配置 DeepSeek API Key，搜索核心不会运行。
