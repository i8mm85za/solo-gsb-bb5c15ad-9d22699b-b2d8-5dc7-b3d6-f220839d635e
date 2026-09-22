# 法规条款变更审阅 Web 应用

上传法规/制度文本的**旧版**与**新版**，系统按**段落顺序 + 字符串相似度**自动生成可解释的
条款对齐候选，识别 **未变更 / 改写 / 新增 / 删除 / 拆分 / 合并 / 多段改写** 并给出置信度；
审阅者可手动调整映射、记录审阅决定与备注、按变更类型/状态/关键字筛选，并导出包含
**两版原文、变更类型、判定依据、审阅决定**的结果文件（CSV / Markdown / JSON）。

技术栈：**Python 3.11 · FastAPI · SQLite（标准库 sqlite3，无 ORM）· 原生 JavaScript/CSS**，
无前端构建步骤，无外部 NLP 依赖。

---

## 快速开始

### 方式一：Docker Compose（推荐）

```bash
docker compose up -d --build
```

- 容器启动时自动初始化 SQLite（`docker/entrypoint.sh`，幂等）。
- 浏览器访问：<http://localhost:8000>
- 数据持久化在 Docker 卷 `regreview-data`（容器内 `/data/regreview.db`）。
- 健康检查：<http://localhost:8000/api/health>

停止：`docker compose down`（加 `-v` 同时删除数据卷）。

### 方式二：Docker

```bash
docker build -t regreview .
docker run -d -p 8000:8000 -v regreview-data:/data --name regreview regreview
```

### 方式三：本地裸跑

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

本地数据库回退到仓库内 `./data/regreview.db`（已在 `.gitignore` 中忽略）。

---

## 使用流程

1. **新建审阅**：填写名称，分别粘贴旧版/新版纯文本（段落之间建议空行分隔；
   `第一条`、`1.`、`（一）` 等编号行会被自动识别为段落边界），点击「生成对齐候选」。
   也可点击「填入示例文本」快速体验。
2. **复核候选**：每张卡片展示
   - 变更类型徽标、**置信度**进度条与段落相似度；
   - **判定依据**（中文可解释说明，含相似度数值）；
   - 旧版/新版原文对照，字符级差异红/绿高亮；
   - 是否为「人工调整」。
3. **记录决定**：选择 **待审阅 / 确认 / 有异议 / 忽略**，填写审阅备注。
4. **调整映射**：
   - 卡片上「✎ 调整映射」可勾选旧/新段落重新建立对应（多旧→一新＝合并，
     一旧→多新＝拆分，也可强制指定变更类型）；
   - 「删除映射」释放段落，工作区顶部「＋ 新建映射」可重新关联未映射段落；
   - 同一段落不允许同时出现在两条映射中（接口返回 409）。
5. **筛选**：顶部统计胶囊按变更类型筛选；下拉按审阅状态筛选；搜索框匹配原文/依据/备注。
6. **重新对齐**：放弃全部人工结果，按当前文本重新计算。
7. **导出**：「⬇ 导出」按当前筛选条件导出
   - **CSV**（UTF-8 BOM，Excel 可直接打开）
   - **Markdown 报告**（人类可读，含每条两版原文引文）
   - **JSON**（结构化全量数据，便于二次处理）

---

## 对齐算法说明（可解释、可复现）

代码见 `app/aligner.py`：

1. **段落切分** `split_paragraphs()`
   先按空行分块；块内若出现多个条款号开头行（`第X条/章/节/款/项`、`1.`、`（一）`）
   再按编号边界细分；无结构文本回退为按非空行切分。
2. **相似度** `similarity()`
   `0.4 × difflib 原文比率 + 0.6 × 去除标点/空白/数字后比率`，对中文标点与排版差异更稳健。
3. **第一遍锚点（weighted LCS）**
   在字符集合 Jaccard 预筛后，对相似度 ≥ 0.88 的候选对做带权最长公共子序列，
   求出总相似度最大且**保持段落顺序**的高置信锚点匹配。
4. **区间对齐（Needleman–Wunsch）**
   锚点之间的未匹配区间做序列对齐（gap −0.6，配对收益由相似度映射到 −1~1）。
5. **合并/拆分识别**
   以配对列为起点贪心吸收相邻的旧段/新段游程：仅当整组相似度**上升且高于阈值 0.45**
   时判为「合并（多旧→一新）」或「拆分（一旧→多新）」；同一段落被竞争时整组相似度高者胜出。
6. **其余**：无对应旧段 → 新增；无对应新段 → 删除；1:1 且相似度 ≥ 0.88 → 未变更；
   ≥ 0.30 → 改写；手动建立的多对多 → 多段改写。
7. **置信度**：由相似度与变更类型映射（未变更 0.99~1.0、改写 0.5~0.92、
   拆分/合并 0.55~0.9、新增/删除 0.75）。
8. 每条记录同时保存 `difflib` 字符级 opcodes 与中文解释，前端做逐字 diff 高亮。

> 这是基于字符串相似度的确定性算法，不依赖任何模型；所有阈值集中在
> `app/aligner.py` 顶部常量，便于按实际语料调参。

## 数据模型（SQLite）

| 表 | 说明 |
|---|---|
| `documents` | 审阅任务：标题、旧版/新版全文、创建/对齐时间 |
| `paragraphs` | 切分后的段落（`side` = old/new，`idx` 为 0 基序号） |
| `alignments` | 对齐候选与审阅结果：段落序号集合、变更类型、置信度、相似度、两版组文本、字符级 diff、判定依据、是否人工、审阅决定/备注/时间 |

## HTTP API

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/documents` | 上传两版文本，自动对齐 |
| GET | `/api/documents` | 文档列表（含段落数/对齐数/已审数） |
| GET | `/api/documents/{id}` | 详情：段落 + 全部对齐记录 |
| DELETE | `/api/documents/{id}` | 删除文档（级联删除） |
| PATCH | `/api/documents/{id}/alignments/{aid}` | 更新审阅决定 / 备注 |
| PUT | `/api/documents/{id}/alignments/{aid}` | 手动调整映射（新建人工记录并替换原记录） |
| POST | `/api/documents/{id}/alignments` | 手动新建映射（冲突返回 409） |
| DELETE | `/api/documents/{id}/alignments/{aid}` | 删除映射 |
| POST | `/api/documents/{id}/realign` | 重新自动对齐 |
| GET | `/api/documents/{id}/export?format=csv\|md\|json` | 导出（可加 `change_type`、`decision` 筛选） |
| GET | `/api/health` | 健康检查 |

交互式文档（Swagger）：<http://localhost:8000/docs>

## 目录结构

```
.
├── app/
│   ├── main.py        # FastAPI 路由：上传/对齐/审阅/导出 + 静态页
│   ├── aligner.py     # 段落切分、相似度、LCS/NW 对齐、合并拆分识别
│   ├── database.py    # SQLite 连接、schema 初始化（容器启动执行）
│   └── schemas.py     # Pydantic 请求/响应模型
├── static/
│   ├── index.html     # 单页应用外壳
│   ├── app.js         # 原生 JS：列表/上传/审阅工作区/映射模态框
│   └── style.css
├── docker/entrypoint.sh   # 初始化数据库后启动 uvicorn
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

## 已知边界

- 段落切分依赖空行或条款编号排版；极端无结构文本会按行切分。
- 算法以「保序的 1:1 主体 + 局部多对一/一对多」为模型；大范围交叉重写可能被保守地
  判为多条改写/新增/删除，审阅者可通过手动映射修正为「多段改写」。
