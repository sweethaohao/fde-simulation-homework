# 门店补货与库存协同 · 智能体 MVP 构建提示词

> **用法**：把本文件整份内容交给编码 Agent（Claude Code / Cursor / Codex 等），不要删减章节。
> **性质**：这是构建指令，不是设计讨论。文中每一条都是硬约束，标注「不得」「禁止」的部分不得自行放宽。
> **本项目的数据全部为合成数据**，无任何真实业务数据，提示词中出现的数字仅用于让演示具备形状。

---

## 0. 你的角色与执行纪律

你是资深 AI 应用工程师。你要在下面指定的仓库里交付一个**可运行、可部署、可演示**的 MVP。

执行纪律，按优先级排列：

1. **不许替换技术栈**，不许引入未列出的中间件。禁止项见第 12 节，违反即返工。
2. **不许问「要不要做」**。文中已给出的决策就是最终决策；只有第 15 节「必须先确认再动手」的条目例外。
3. **不写解释性长文**。每阶段只给四件套：① 变更文件清单 ② 启动与自测命令 ③ 可直接跑通的验证脚本 ④ 本阶段最可能出问题的地方。
4. **不许伪造数据来源**。合成数据必须在库里、接口里、界面上三处都标明 `synthetic`。
5. **不许跳过验证**。每个阶段结束前，四件套里的验证脚本必须你自己跑通并贴出真实输出。

---

## 1. 环境事实（已实测，不得假设、不得改动）

| 项 | 事实 | 说明 |
|---|---|---|
| 操作系统 | Windows（Git Bash 可用） | 脚本同时给 `.bat` 与 `.sh` 两份 |
| 仓库根目录 | `G:\project\homework\开源-fde模拟作业\开源课程-FDE模拟提交` | 所有代码写在这里 |
| Python | **3.13.14**，虚拟环境已建于 `./.venv` | 不得降版本，不得另建 venv |
| 已装依赖 | 见仓库根 `requirements.txt`（已实际安装并导入验证通过） | fastapi 0.165.0 / uvicorn 0.54.0 / pydantic 2.14.3 / httpx 0.28.1 / PyYAML 6.0.3 / PyJWT 2.15.1 |
| 数据库 | 标准库 `sqlite3`，SQLite **3.53.1** | 已实测支持 FTS5、JSON1、**递归 CTE** |
| Ollama | `G:\Ollama\ollama.exe`，版本 **0.40.16**，服务已在 `127.0.0.1:11434` 运行 | 若未运行，用 `G:\Ollama\start-serve.bat` 拉起；**不要修改该 bat** |
| 生成模型 | `qwen3.5:9b`（6.55 GB，9B） | **思考模型**：默认把内容写进 `thinking`，`message.content` 为空。**每次调用必须带 `"think": false`** |
| 嵌入模型 | `nomic-embed-text:latest`（274 MB） | `/api/embed` 返回 **768 维**，无需任何启动参数 |
| 显卡约束 | RTX 5070 Laptop 8 GB 显存，9B 约 13% 溢出到 CPU，实测 30–37 tokens/s | 上下文控制在 `num_ctx ≤ 8192`；单次送入 LLM 的检索片段不超过 6 段、总字符 ≤ 6000 |
| 局域网地址 | `10.51.79.169`（WLAN，/24） | 部署后用 `http://10.51.79.169:8000` 访问 |
| 端口 | `8000` 空闲 | 前端由 FastAPI 静态托管，不另起服务 |
| 公网出口 | 受限：`api.ipify.org` TLS 握手失败；本机无 cloudflared / ngrok | **不要把公网访问作为验收前提**，见第 11 节 |

> 已核对清楚的一点：`ollama serve` 在本版本**没有** `--embeddings` 参数，加上会报 `unknown flag`。嵌入能力由模型本身决定（`nomic-embed-text` 直接可用），不要再去试这个参数。

---

## 2. 项目背景与系统定位

**案例**：拾光连锁「门店补货与库存协同」，48 家门店一边缺货、一边积压。客户最初的诉求是「用 AI 预测销量、自动补货」。

**上游调研已经推翻了这个假设**：缺货的主因不是预测不准，而是「可售库存这个数没有可信定义」——系统算有库存、货架是空的；POS 按日汇总、WMS 实时出库，粒度不一致；缺货标识谁维护没人说得清。

**所以 MVP 的定位是一句话**：

> 一套**可售库存诊断与补货建议**系统。它负责把「测不准」变成「可测、可溯源、可签核」，**不负责下单**。

三条不可让的设计约束，任何一条被破坏就算没做完：

1. **只读 + 建议 + 待签核**：系统不存在「创建订单」的代码路径，只有「创建待签核单据」（对应 f44 / 零售-033）。
2. **兜底优先于智能**：命中兜底条件时，服务端必须返回 `stopped` 并给出原因、接手人、人工确认时间，**不返回建议量**。这条在服务端强制，不靠前端隐藏。
3. **每个判断可溯源**：任意一条建议、一个指标值，都要能回答「依据哪条规则、哪条证据、谁负责」。这是知识图谱存在的唯一理由。

---

## 3. 用户与权限（必须包含管理员）

七个角色，全部落库，全部可用账号登录演示。

| 角色代码 | 角色 | 对应人 | 数据域 | 允许的动作 | 明确禁止 |
|---|---|---|---|---|---|
| `admin` | **系统管理员** | — | **全局** | **查看全部内容**（所有门店、所有单据、所有日志、全部图谱）；用户管理（增删改、重置密码、启停用）；审计导出 | **不得签核业务单据、不得改指标口径、不得改兜底规则**（保留人在环） |
| `ops_director` | 运营总监 | 顾明岚 | 全局 | 看全部指标、签发指标口径、决定停止/继续、指定改进负责人（f55） | 不得绕过审计写入 |
| `loss_manager` | 商品与损耗经理 | 傅景 | 全局 | 风险否决、兜底配置、审计日志查阅、评价记录处置（f43） | 不得改指标口径 |
| `planner` | 补货计划员 | 唐映秋 | 本区域 | 生成/修改建议量、提交待签核单据、核实下游反馈（f54） | **不得下单**、不得改促销规则 |
| `store_manager` | 门店店长 | 林亦舟 | 本店 | 缺货上报、查看本店建议与到货、签收确认 | 不得看他店数据、不得跳签核 |
| `logistics` | 区域物流经理 | 叶书 | 本区域 | 查看配送衔接与分配依据、受理投诉、给出门店可见的预计到货与未满足数量（f52） | 不得改截单规则 |
| `data_engineer` | 数据工程师 | 秦芮 | 全局只读 | 数据导入、字段口径维护、刷新状态、查看数据就绪度 | 不得改业务规则、不得替业务签字 |

**权限实现规则**（这是评分点，必须能演示）：

- 三层判定：**角色 → 数据域 → 动作**。三层全过才放行，任一层不过返回 403 并写入审计。
- `admin` 的「查看全部」是**放宽数据域**，不是放宽动作：`admin` 调用签核接口必须返回 `FORBIDDEN_ACTION`，且审计里记一条「管理员尝试签核被拒」。
- 否决权与目标权分离：`loss_manager` 能否决，`ops_director` 能停，两者不可互相覆盖。
- 演示必须能现场切换账号，看到**同一页面不同角色可见范围不同**（例如店长只看本店 8 家中的 1 家，admin 看全部 8 家）。

种子账号（密码统一 `Demo@2026`，首次登录不强制改密，界面标注为演示账号）：

```
admin / admin@shiguang.local
gu.minglan / ops@shiguang.local        (ops_director)
fu.jing / loss@shiguang.local          (loss_manager)
tang.yingqiu / planner@shiguang.local  (planner, 区域=华东)
lin.yizhou / store@shiguang.local      (store_manager, 门店=S01)
ye.shu / logistics@shiguang.local      (logistics, 区域=华东)
qin.rui / data@shiguang.local          (data_engineer)
```

密码哈希用标准库 `hashlib.scrypt`（`n=2**14, r=8, p=1, dklen=32`，每用户独立 16 字节盐，存 `salt$hash`）。**不要引入 bcrypt / passlib**。JWT 用 PyJWT，HS256，密钥从环境变量 `APP_JWT_SECRET` 读取，未设置时启动直接失败（不许有硬编码兜底密钥）。

---

## 4. 数据红线（R1–R10，逐条落到代码位置）

| # | 红线 | 落点（必须能指出文件与函数） | 验收方式 |
|---|---|---|---|
| R1 | 不自动下单 | 订单服务只暴露 `create_pending_order()`，全仓 grep 不到任何外部下单调用 | `grep -rn "place_order\|submit_order" src/` 应只命中注释 |
| R2 | 兜底触发即停建议 | `guardrail.evaluate()` 在生成建议**之前**执行，命中即短路返回 `stopped` | 构造 f44 五类各一条用例，断言 `data.suggestion` 为 `null` |
| R3 | 数据只读 | 外部数据仅 CSV 导入；无任何写回客户系统的代码 | 导入函数只做 `INSERT` 到本地库 |
| R4 | 不记口令与非必要个人信息 | 审计写入前过字段白名单，`password/token/phone/id_card` 一律丢弃 | 单测：传入含 token 的字典，落库后不含该值 |
| R5 | 审计可重建 | 每次建议留存：销售库存快照、促销版本、整箱规格版本、配送日历版本、建议内容、人工修正、签核人 | 打开任一已签核单据的「重建视图」能还原全过程 |
| R6 | 日志留存 90 天 | `audit_retention_days=90`，到期归档；处置需 `loss_manager` 确认，不自动续期 | `GET /api/audit/retention` 返回剩余天数 |
| R7 | 促销期单列 | 促销窗口数据打 `is_promo` 标签，默认不进基线统计 | 看板切换「含/不含促销」数值不同 |
| R8 | 不增库存、不改合同 | 无采购下单、无合同写接口 | 路由表里不存在相关端点 |
| R9 | 接口未审批前只导 CSV | 无供应商接口客户端（f32） | `grep -rn "requests\.\|httpx\." src/` 只允许命中 Ollama 客户端 |
| R10 | 指标分母由业务负责人签发 | `metric_definitions.signed_by` 为空时，指标接口返回 `METRIC_BLOCKED`，不出数 | 演示：切到未签发的「周转天数」，界面显示阻塞原因而非数字 |

---

## 5. 技术栈（锁定，不得替换）

```
Python 3.13.14
FastAPI + uvicorn
pydantic v2
SQLite（标准库 sqlite3，文件库）
Ollama 本地部署：qwen3.5:9b（生成） + nomic-embed-text（768 维向量）
原生 HTML + CSS + JavaScript 单页前端（无构建、无 npm、无框架）
```

**Harness 定位**：Harness 在本项目中是「让 Agent 更好工作的编排治理层」，不是 CI/CD，也不是 pip 依赖。落到 MVP 里就是三样东西，**不要因此 import 任何 SDK**：

1. Agent 与工具的**注册表**（`src/app/agents/registry.py`，声明四个 Agent 与七个工具及其权限）
2. **调用留痕**（每次 LLM/工具调用写 `agent_runs` 表：输入摘要、耗时、token、结果状态）
3. **健康与就绪**（`/api/health`、`/api/ready` 分别报告进程与依赖状态）

---

## 6. 目录结构（照此创建，不要自创层级）

```
开源课程-FDE模拟提交/
├── requirements.txt                 # 已有，不要改版本
├── config.yaml                      # 新建：全部可配置项，禁止硬编码
├── .env.example                     # 新建：APP_JWT_SECRET 等
├── README.md                        # 追加「运行与部署」章节
├── docs/                            # 已有平台材料 + 分析文档，不要动
├── data/
│   ├── mvp.db                       # 运行时生成，加入 .gitignore
│   └── seed/                        # 合成数据产物（CSV/JSON），可提交
├── scripts/
│   ├── gen_data.py                  # 生成 8 店 × 50 SKU × 42 天合成数据
│   ├── init_db.py                   # 建表 + 灌种子（幂等，可重复跑）
│   ├── seed_graph.py                # 把规则/证据/角色/环节灌入图谱
│   ├── run_dev.bat / run_dev.sh     # 开发启动
│   ├── run_prod.bat / run_prod.sh   # 生产启动（0.0.0.0:8000）
│   └── smoke.py                     # 一条命令跑完全链路冒烟
├── src/
│   ├── app/
│   │   ├── main.py                  # FastAPI 入口、静态托管、异常中间件
│   │   ├── config.py                # 读 config.yaml + 环境变量覆盖
│   │   ├── errors.py                # 错误码枚举 + 统一响应构造
│   │   ├── security.py              # scrypt 哈希、JWT 签发校验
│   │   ├── deps.py                  # 认证依赖、角色/数据域/动作三层校验
│   │   ├── db.py                    # 连接管理、事务、行工厂
│   │   ├── llm/
│   │   │   ├── ollama.py            # chat / embed 客户端，强制 think=false
│   │   │   └── prompts.py           # 所有提示词模板集中在此
│   │   ├── agents/
│   │   │   ├── registry.py          # Agent 与工具注册表（Harness 落点）
│   │   │   ├── diagnose.py          # 诊断 Agent：缺货三因拆分
│   │   │   ├── suggest.py           # 建议 Agent：建议量 + 依据
│   │   │   ├── trace.py             # 溯源 Agent：三跳图谱查询
│   │   │   └── guardrail.py         # 兜底 Agent：五类硬拦截
│   │   ├── services/
│   │   │   ├── metrics.py           # 指标计算与 blocked 判定
│   │   │   ├── graph.py             # GraphStore 抽象 + SQLite 实现
│   │   │   ├── retrieval.py         # 向量检索 + FTS5 降级
│   │   │   ├── replenishment.py     # 建议 → 待签核单据 → 签核留痕
│   │   │   └── audit.py             # 审计写入、白名单过滤、导出
│   │   └── routers/
│   │       ├── auth.py  dashboard.py  metrics.py  graph.py
│   │       ├── replenishment.py  admin.py  audit.py  health.py
│   └── web/
│       ├── index.html               # 单页：登录 + 五个视图
│       ├── app.js                   # 原生 JS，fetch 封装统一响应
│       └── style.css                # 无外链 CDN，离线可用
└── tests/
    ├── test_auth_rbac.py            # 含 admin 越权用例
    ├── test_guardrail.py            # f44 五类
    ├── test_metrics_blocked.py      # R10
    ├── test_replenishment_flow.py   # 建议→签核→留痕
    └── test_audit_redaction.py      # R4
```

---

## 7. 数据合成要求（无真实数据，全部由你生成）

生成脚本 `scripts/gen_data.py`，**固定随机种子 `20261119`**，输出可复现。

**规模**

| 实体 | 数量 | 说明 |
|---|---|---|
| 门店 | 8（S01–S08） | 华东区域，分高/中/低缺货三档（3/3/2） |
| SKU | 50 | 常温品，分爆款 10 / 常规 25 / 长尾 15 |
| 日期 | 42 天（6 周） | 前 3 周「历史回放」，后 3 周「并行运行」 |
| 日快照 | 8 × 50 × 42 = 16,800 行 | 账面库存、货架可售量、在途、后仓存量 |
| 补货单 | 约 600 张 | 含建议量、取整后量、实际发出量、签收量 |
| 事件流 | 约 5,000 条 | 见下表 |

**事件类型（必须全部具备，时间戳按实际情况留空）**

| 事件 | 有可信时间戳 | 备注 |
|---|---|---|
| `stockout_detected` 发现缺货 | **30% 留空** | 无系统记录，靠人目视（f10/f13），留空正是演示 `data_readiness` 的材料 |
| `report_to_hq` 门店报数 | 50% 有 | 群聊/电话，渠道字段取值 `group_chat/phone/system`（f14） |
| `suggestion_generated` 生成建议 | 有 | 系统事件 |
| `rounded_to_case` 整箱取整 | 有 | 记录取整前后量（f20） |
| `capacity_capped` 仓容压回 | 有 | 记录压回量，制造「一边缺一边压」（f20） |
| `transfer_approval` 跨店调拨审批 | 60% 有 | 审批人写「区域经理」，但该角色**未受访**，字段标 `unverified`（f22） |
| `cutoff` 截单 | 有 | 截单时点作为配置项，不写死 |
| `outbound` 出库 | 有 | WMS 实时口径 |
| `delivered` 签收 | 有 | 唯一可信的交接时间（f52） |
| `shelved` 上架 | **全部留空** | 无记录，正是「有货不可售」发生处（f10） |
| `returned_for_missing_info` 要件不全退回 | 30% 有 | 叶书受理、计划员核实（零售-034 / f54） |

**刻意植入的分布**（用于让数字有意义，不是让数字好看）

- 缺货率：按「缺货 SKU 数 / 在售 SKU 数」算，8 店合计约 **12%**，但**换分母（按时长加权）会变成约 21%**——这个差异本身是演示内容，不要抹平。
- 账实差异：约 **8%**，其中约 1/3 由「新旧商品码」造成的伪差异构成（f30）。
- 延迟归因：延迟单中约 **30%** 归「配送排队」，其余归「仓容压回」「整箱取整」「要件退回」（f51）。
- 周转天数：约 **48 天**，但含不含在途会让结果差 6 天以上 → 口径未签发，故默认 `METRIC_BLOCKED`。

**硬性要求**

1. 所有表加 `is_synthetic INTEGER DEFAULT 1`，接口返回体带 `"synthetic": true`，界面顶部常驻黄色横幅「本系统全部数据为合成数据，仅用于演示」。
2. 生成脚本必须幂等：重复执行结果一致（先 `DELETE` 再插入，或整体重建库）。
3. 时间戳统一存 ISO 8601 字符串（`2026-09-01T08:30:00+08:00`），空值存 `NULL`，**不要用 `0` 或空字符串冒充**。

---

## 8. 知识图谱

**存储**：SQLite 两张表 + 递归 CTE，不装 Neo4j。抽象成 `GraphStore` 接口（`get_node / neighbors / path / search`），将来换图库只改实现。

```sql
CREATE TABLE graph_nodes (
  id TEXT PRIMARY KEY,            -- 形如 rule:f44 / evidence:f44 / role:loss_manager
  kind TEXT NOT NULL,             -- rule | evidence | role | step | signal | doc
  name TEXT NOT NULL,
  props TEXT NOT NULL DEFAULT '{}',   -- JSON1
  created_at TEXT NOT NULL
);
CREATE TABLE graph_edges (
  src TEXT NOT NULL, dst TEXT NOT NULL,
  rel TEXT NOT NULL,              -- EVIDENCED_BY | OWNED_BY | APPLIES_TO | TRIGGERS | PRECEDES | SIGNED_BY
  props TEXT NOT NULL DEFAULT '{}',
  PRIMARY KEY (src, dst, rel)
);
```

**必须能跑通的一条三跳查询**（这是「知识图谱」的验收证据，不是装饰）：

```text
suggestion:{id} -EVIDENCED_BY-> evidence:f44 -OWNED_BY-> role:loss_manager
```
即：这条建议 → 依据哪条规则/证据 → 该证据的责任人是谁。递归 CTE 写成一条 SQL，把路径上的 `kind/name/rel` 全部返回，前端画成三列连线（纯 CSS/SVG，不引第三方库）。

**另外两个必答查询**

- 某条规则（如 `rule:f44`）影响哪些环节、由谁签核 → 反向两跳。
- 某指标（如 `metric:stockout_rate`）的口径由谁签发、当前是否已签 → `SIGNED_BY` 边。

---

## 9. 规则种子（原样入库，`f` 编号不得改写、不得新增）

从 `docs/discovery-report.md` 与 `docs/作业内容分析/` 中提取，写入 `scripts/seed_graph.py`。**下列条目是必须存在的**，其余可自行补全：

| 编号 | 内容（摘要） | 节点类型 | 责任人 |
|---|---|---|---|
| f00 | 目标：缺货 12%→5%、周转 48→35 天；「对上面交的账」 | signal | 顾明岚 |
| f01 | 缺货标识维护责任未落地，需秦芮确认数据可得性 | rule | 顾明岚 |
| f02 / f05 | 试点 8 店、周期 6 周 | rule | 顾明岚 |
| f03 | 预算 16 万封顶，含四项成本 | rule | 顾明岚 |
| f10 | 货在后仓未上架，系统仍算有库存 | evidence | 林亦舟 |
| f11 | 临时促销变价使预测失效 | evidence | 林亦舟 |
| f12 | 每天盘 20 个重点 SKU，账实差异平均 8% | signal | 林亦舟 |
| f13 | 发现缺货无时点记录 | evidence | 林亦舟 |
| f14 | 群聊/个人表补说明再回系统补录，关键原因无结构化字段 | rule | 林亦舟 |
| f20 | 最小起订量、整箱规格、仓库分配上限导致压回 | rule | 唐映秋 |
| f21 | 促销版本运营下发，计划员只拿到下发版 | rule | 唐映秋 |
| f22 | 跨店调拨需区域经理批准；自动采购不准上线 | rule | 唐映秋 |
| f23 | 发送消息不等于责任已转移 | rule | 唐映秋 |
| f24 | 规则维护与争议裁定归属；门店改价 | rule | 唐映秋 |
| f25 | 超时交运营总监协调 | rule | 唐映秋 |
| f30 | POS 日汇总与 WMS 实时粒度不一致，编码有新旧码/套装码 | evidence | 秦芮 |
| f31 | 缺货截断数据不可信 | evidence | 秦芮 |
| f32 | 供应商接口尚未审批 | rule | 秦芮 |
| f33 / f34 | 建议量与规则须留版本 | rule | 秦芮 |
| f35 | 业务编号去重规则 | rule | 秦芮 |
| f42 | 三指标同看、促销期单列；周转报废反向恶化不认 | rule | 傅景 |
| f43 | 评价记录暂留 90 天，到期由商品与损耗经理确认删除或归档 | rule | 傅景 |
| **f44** | **兜底五类：临时促销、缺货截断、可售与账面不一致、依据缺失、关键系统不可用 → 停止自动建议，标明原因、接手人、人工确认时间** | **rule（硬拦截）** | 傅景 |
| f45 | 审核日志须留存六类要素，可重建；不记口令与非必要个人信息 | rule | 傅景 |
| f51 | 约 30% 延迟补货源于配送排队 | signal | 叶书 |
| f52 | 不改供应商合同；须给门店看预计到货与未满足数量 | rule | 叶书 |
| f53 | 三指标分母/周期/排除规则由业务负责人固定；不能用消息发送量代替实际完成量 | rule | 叶书 |
| f54 | 下游反馈须关联原业务编号与资料版本，叶书受理、计划员核实 | rule | 叶书 |
| f55 | 每周复盘，改进负责人由运营总监指定，复测达标才更新规则 | rule | 顾明岚 |
| 零售-032/033 | 兜底触发率与日志完整率是否一票否决待定；审批矩阵与代签规则待定 | rule（`status=unverified`） | 傅景 |
| 零售-034 | 下游反馈缺编号/版本即退回重问，全程不留痕 | evidence | 叶书 |

**标注纪律**：`f` 编号是「已证」；材料未记录的写 `unverified` 并在 `props.note` 说明向谁确认。**不得把推测写成事实，也不得新增 f 编号。**

---

## 10. MVP 功能清单（P0 全部必做）

| # | 功能 | 必须做到 |
|---|---|---|
| F1 | 登录与 RBAC | JWT + scrypt；三层校验；`/api/auth/me` 返回角色与数据域 |
| F2 | 诊断看板 | 缺货率**两种分母并列展示**并标出口径差异；账实差异按「真差异/编码伪差异」拆分；时效指标在数据不足时显示 `blocked` 及原因，**不许用估算值填空** |
| F3 | 图谱溯源 | 三跳查询可视化；点任一数字能追到规则 → 证据 → 责任人 |
| F4 | 建议生成 | LLM 产出建议量 + 依据 + 置信说明；无 LLM 时用规则兜底并标注 `degraded` |
| F5 | 兜底判定 | f44 五类服务端硬拦截，返回 `stopped` + 原因 + 接手人 + 人工确认时间 |
| F6 | 待签核单据 | 生成 → 提交 → 签核/驳回 → 留痕；`admin` 签核被拒 |
| F7 | 审计与导出 | 每次读敏感数据、每次签核、每次越权尝试都落库；支持 CSV 导出与单条重建视图 |
| F8 | 管理员视图 | 全局看板 + 用户管理 + 审计导出 + 数据就绪度；不可签核 |
| F9 | 单页前端 | 登录、看板、溯源、建议与签核、管理与审计五个视图；无 CDN 外链 |

**明确不做（写进 README 的 Out of Scope）**：销量预测模型、自动下单、供应商接口对接、移动端适配、多租户、消息队列、缓存层。

---

## 11. 接口契约

**统一响应**（所有 `/api/*`，含错误）

```json
{ "ok": true,  "data": {}, "error": null }
{ "ok": false, "data": null, "error": { "code": "GUARDRAIL_STOPPED", "msg": "命中兜底：临时促销生效中" } }
```

HTTP 状态码与 `ok` 同时反映结果：业务阻塞用 **200 + ok=false**（`GUARDRAIL_STOPPED`、`METRIC_BLOCKED`、`DATA_NOT_READY`），认证失败 401，权限不足 403，校验失败 422，未找到 404。

**错误码枚举**（`errors.py`，禁止散落字符串）

```
AUTH_REQUIRED  AUTH_INVALID  FORBIDDEN_SCOPE  FORBIDDEN_ACTION
VALIDATION_ERROR  NOT_FOUND  CONFLICT_ALREADY_SIGNED  SELF_SIGN_FORBIDDEN
GUARDRAIL_STOPPED  METRIC_BLOCKED  DATA_NOT_READY  LLM_UNAVAILABLE
IMPORT_FAILED  INTERNAL
```

**端点**

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/auth/login` | 账号密码换 token |
| GET | `/api/auth/me` | 当前身份、角色、数据域 |
| GET | `/api/health` `/api/ready` | 进程存活 / 依赖就绪（db、ollama） |
| GET | `/api/dashboard/summary` | 看板聚合，按数据域过滤 |
| GET | `/api/metrics/{code}` | 单指标，未签发返回 `METRIC_BLOCKED` |
| GET | `/api/metrics/{code}/trace` | 指标 → 规则 → 责任人 三跳 |
| GET | `/api/data/readiness` | 各指标数据就绪度与缺失项 |
| POST | `/api/replenishment/suggest` | 生成建议（先过兜底） |
| GET | `/api/replenishment/orders` | 待签核列表 |
| POST | `/api/replenishment/orders/{id}/sign` | 签核（admin 拒绝） |
| POST | `/api/replenishment/orders/{id}/reject` | 驳回并记原因 |
| GET | `/api/replenishment/orders/{id}/rebuild` | R5 重建视图 |
| GET | `/api/graph/path?src=&rel=&depth=` | 图谱路径查询 |
| GET | `/api/graph/search?q=` | 向量 + FTS5 混合检索 |
| GET | `/api/admin/users` `POST` `PATCH` | 用户管理（仅 admin） |
| GET | `/api/audit/logs` `/api/audit/export` | 审计查询与导出（admin / loss_manager） |
| GET | `/api/guardrail/rules` | 兜底规则清单及其 `f` 证据 |

---

## 12. 禁止事项（违反即返工）

1. 禁止替换 Web 框架、禁止引入 SQLAlchemy / Alembic / Django / Flask。
2. 禁止引入 Redis / Kafka / RabbitMQ / Celery / Docker Compose 额外服务。
3. 禁止安装 Neo4j 或任何向量数据库（Chroma / FAISS / Milvus / pgvector 全禁）。
4. 禁止使用 npm / 打包工具 / 前端框架（React / Vue / Tailwind CDN 都不行），前端必须离线可跑。
5. 禁止把密钥、模型地址、端口、库路径写进代码；全部走 `config.yaml` + 环境变量。
6. 禁止硬编码兜底判定结果、禁止为了「跑通演示」把兜底短路掉。
7. 禁止用估算值填充 `blocked` 指标。
8. 禁止编造 `f` 编号，禁止把 `unverified` 写成已确认。
9. 禁止在代码里 `import` 任何名为 harness 的包（本项目不需要）。
10. 禁止修改 `docs/` 下的平台导出文件。

---

## 13. 代码规范

1. **每个函数写中文 docstring**，格式固定三段：`入参` / `出参` / `失败时的行为`（抛什么异常、返回什么错误码）。
2. **安全审查、权限校验、人工确认**三处必须写中文行内注释，说明**为什么这么卡**（引用 `f` 编号），不要复述做了什么。
3. **每行代码加注释**（用户硬要求）：非空且非纯括号的行都要有中文注释；同一逻辑块的连续赋值可用块首注释覆盖。`import` 区允许按分组注释。
4. 配置集中在 `config.yaml`，代码中只出现 `settings.xxx`；密钥与端口不落代码。
5. 所有对外接口返回第 11 节的统一结构，异常由全局中间件兜底，不允许裸 `raise HTTPException` 泄漏堆栈给前端。
6. 错误码用枚举；日志用标准库 `logging`，JSON 结构化，字段含 `request_id`。
7. 时间一律带时区，SQLite 存 ISO 8601 字符串；金额用整数分，不要浮点。

**`config.yaml` 必须包含的键**（值按第 1 节环境填）：

```yaml
app:
  name: "门店补货与库存协同 · MVP"
  host: "0.0.0.0"
  port: 8000
  env: "prod"
  data_scope_default: "store"      # 缺省最小数据域
db:
  path: "data/mvp.db"
llm:
  base_url: "http://127.0.0.1:11434"
  chat_model: "qwen3.5:9b"
  embed_model: "nomic-embed-text"
  think: false                     # 必须 false，否则 content 为空
  temperature: 0.2
  num_ctx: 8192
  timeout_seconds: 120
  max_context_chars: 6000          # 单次送入 LLM 的检索片段总量上限
retrieval:
  top_k: 6
  vector_dim: 768
  fallback: "fts5"                 # 无嵌入模型时降级，不报错
auth:
  jwt_secret_env: "APP_JWT_SECRET"
  token_ttl_minutes: 480
  password_hash: "scrypt"
guardrail:
  enabled: true
  stop_actions_on_hit: true        # 命中即停，不返回建议量
audit:
  retention_days: 90
  redact_fields: ["password", "token", "secret", "phone", "id_card"]
metrics:
  signed_required: true            # 未签发不出数（R10）
demo:
  synthetic_banner: true
```

---

## 14. 部署与上线（验收硬要求）

1. `scripts/run_prod.sh` 与 `scripts/run_prod.bat`：
   - 先检查 `127.0.0.1:11434` 是否可达，不可达时打印 `G:\Ollama\start-serve.bat` 提示并退出码 2；
   - 再检查 `APP_JWT_SECRET`，未设置则提示从 `.env.example` 复制，退出码 3；
   - 最后 `uvicorn src.app.main:app --host 0.0.0.0 --port 8000 --workers 2`。
2. 首次部署顺序写进 README：`gen_data.py` → `init_db.py` → `seed_graph.py` → `run_prod`。
3. 访问地址：本机 `http://127.0.0.1:8000`；同网段 `http://10.51.79.169:8000`。README 中给出 Windows 防火墙放行 8000 端口的命令（`netsh advfirewall firewall add rule`）。
4. `/api/ready` 必须真实探测数据库与 Ollama，任一不通返回 503 + 具体原因，**不许永远返回 200**。
5. 公网访问不作为验收前提（本机出口受限）。README 写明：如需公网，自行安装 cloudflared 隧道，失败不影响本机与局域网验收。
6. 交付时提供一段可复制的演示动线（登录 admin → 看全部 → 切店长只看本店 → 触发兜底 → 签核被拒 → 溯源三跳），不超过 8 步。

---

## 15. 必须先确认再动手的三件事

只有这三件允许向我要确认，其余一律自己定：

1. **是否允许把 `data/mvp.db` 与 `data/seed/*.csv` 提交进 git**（我倾向：seed 提交、db 不提交）。
2. **签核是否需要二次确认口令**（我倾向：不要，MVP 用按钮 + 确认弹窗即可）。
3. **演示是否需要保留「故意失败」的快照**（我倾向：保留，兜底被触发的那条单据不要清理，作为验收证据）。

---

## 16. 验收清单（交付时逐条打勾，附证据）

| # | 验收项 | 证据 |
|---|---|---|
| 1 | 七个角色可登录，权限边界正确 | 七次登录截图 + `test_auth_rbac.py` 全绿 |
| 2 | admin 能看全部、不能签核 | 签核返回 `FORBIDDEN_ACTION` 的响应体 + 审计记录 |
| 3 | f44 五类兜底全部拦得住 | `test_guardrail.py` 五条用例 |
| 4 | 未签发指标不出数 | `GET /api/metrics/turnover_days` 返回 `METRIC_BLOCKED` |
| 5 | 建议 → 待签核 → 签核 → 重建 全链路 | 一次完整单据的 `rebuild` 输出 |
| 6 | 三跳溯源可用 | 一条路径的 SQL 与界面截图 |
| 7 | 审计不含敏感字段 | `test_audit_redaction.py` |
| 8 | 服务以 `0.0.0.0:8000` 跑起来，局域网可访问 | `curl http://10.51.79.169:8000/api/health` |
| 9 | 断网可用（无 CDN 依赖） | 断网后刷新页面仍能登录与查看 |
| 10 | 全库无硬编码密钥/地址 | `grep -rn "11434\|sk-\|secret" src/` 只命中配置读取 |

---

## 17. 交付格式（每阶段固定四件套）

```
① 变更文件清单：路径 + 一句话作用 + 新增/修改
② 启动与自测命令：可直接复制，含 .venv 的绝对路径
③ 验证脚本：一段能直接跑通的代码或命令，附真实输出
④ 本阶段最可能出问题的地方：1–3 条，写清症状与快速定位方法
```

**建议分四阶段推进，每阶段结束停下等我确认**：

| 阶段 | 范围 | 出口标准 |
|---|---|---|
| 一 | 骨架 + 契约：config、统一响应、错误码、RBAC、健康检查、用户种子 | 七个账号能登录，越权返回 403 |
| 二 | 数据 + 图谱：合成数据、建表、规则入库、三跳查询 | `smoke.py` 能查出 `suggestion → f44 → 傅景` |
| 三 | 主链路：兜底 → 建议 → 待签核 → 签核留痕 → 审计 | f44 五类拦得住，签核后可重建 |
| 四 | 前端 + 部署：单页五视图、生产启动、README、验收清单 | `10.51.79.169:8000` 可打开并完成演示动线 |

---

## 18. 一句话复述（开工前用一句话确认你理解了）

> 这不是一个「替门店补货」的系统，而是一个**让别人不敢再用一个没有口径的数字做决策**的系统：所有数据都是合成的，所有判断都要能追到 `f` 编号和人，所有动作都停在一张待签核单据上，管理员能看见全部但一个字也签不了。
