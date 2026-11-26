# DEMO · 门店补货与库存协同 MVP

> 全部数据为合成数据；服务只到「待签核 → 人工签核」，不存在下单接口，不回写任何外部系统。

## 1. 启动

```powershell
# 首次：建依赖、密钥、数据、图谱
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env    # 填 APP_JWT_SECRET（至少 16 字符）
.\.venv\Scripts\python.exe scripts\gen_data.py
.\.venv\Scripts\python.exe scripts\init_db.py
.\.venv\Scripts\python.exe scripts\seed_graph.py

# 演示
scripts\run_prod.bat           # 本机 http://127.0.0.1:8000，局域网 http://<本机IP>:8000
```

打开页面后，登录框下方有七个演示账号按钮，点击即填充；统一口令 `Demo@2026`。

## 2. 演示账号

| 账号 | 姓名 | 角色 | 数据域 | 演示看点 |
|---|---|---|---|---|
| `gu.minglan` | 顾明岚 | 运营总监 | 全局 | 签核；未签发指标必须找她 |
| `tang.yingqiu` | 唐映秋 | 补货计划员 | 全局 | 生成建议、提交待签核 |
| `fu.jing` | 傅景 | 商品与损耗经理 | 全局 | 驳回；兜底接手人；审计归档确认人 |
| `lin.yizhou` | 林亦舟 | 门店店长 | S01 | 只看本店；越域被拒 |
| `ye.shu` | 叶书 | 区域物流经理 | 华东 | 配送到店与交接 |
| `qin.rui` | 秦芮 | 数据工程师 | 全局 | 导入批次、就绪度、Agent 注册表 |
| `admin` | 系统管理员 | 管理员 | 全局 | 账号与审计；尝试签核会被拒并留痕 |

## 3. 八步演示动线

1. **登录看板（顾明岚）**：顶部常驻“合成数据”横幅；缺货率两张卡并列 12.0%（42/350，当日截面）与 20.8%（42 天时段加权），差约 9pp，不抹平；账实差异 8.0%，其中约三分之一是编码伪差异。
2. **看被拦截的指标**：库存周转天数显示 `METRIC_BLOCKED`、需要顾明岚签发、没有任何数字；上架时效显示 `DATA_NOT_READY`（shelved 无时间戳）。这是 R10：口径未签发不出数。
3. **下钻溯源**：点数字卡进入“图谱溯源”，看指标 → 规则/证据 → 责任人；也可在“路径查询”选一张建议单，跳数 3，看到 `suggestion → rule:fXX → role:...`。
4. **混合检索**：搜“临时促销 整箱 仓容”，显示 `hybrid` 或降级为 `fts5` 及原因；结果都带 f 编号。
5. **生成建议（唐映秋）**：选 S01/K010（或 K011–K020），点“生成建议”；看到建议量、整箱取整、起订量、仓容压回、置信度、依据 f 编号与检索模式。本地模型不可用时会显示 `degraded` 规则基线。
6. **提交与签核（唐映秋 → 顾明岚）**：计划员点“提交”，单据变“待签核”；换顾明岚登录，点“签核”成功。重复签核返回 `CONFLICT_ALREADY_SIGNED`；管理员点签核返回 `FORBIDDEN_ACTION` 且审计留痕。
7. **五类兜底（任计划员）**：依次点样例按钮“临时促销 / 缺货截断 / 可售与账面不一致 / 依据缺失（整箱规格）/ 关键系统不可用”，每次生成都返回 `GUARDRAIL_STOPPED`，不返回建议量，接手人傅景，并生成 STOPPED 单；在“详情/重建”里能看到命中规则、快照、四类版本、事件流与审计。
8. **管理与审计（admin）**：账号表 7 行；审计表可过滤与导出 CSV（口令、token 等敏感字段已脱敏）；数据就绪度按指标拆项；Agent 注册表显示 4 个 Agent、7 个工具与 Harness 落点；兜底规则展示 f44 五类与 120 分钟人工确认。

## 4. 五类兜底样例（确定性）

| 类别 | 门店/商品 | 触发依据 | 期望错误码 |
|---|---|---|---|
| G1 临时促销 | S01 / K001 | 最后一天命中促销版本 P2026-10-temp-v1 | `G1_TEMP_PROMO` |
| G2 缺货截断 | S02 / K002 | 近 7 天销量 0 且缺货，销量被截断 | `G2_STOCKOUT_TRUNCATION` |
| G3 可售与账面不一致 | S03 / K003 | 账面有货、货架不可售 | `G3_BOOK_SHELF_MISMATCH` |
| G4 依据缺失 | S04 / K049 | 整箱规格为空 | `G4_MISSING_EVIDENCE` |
| G4 依据缺失 | S08 / K005 | 门店配送日历版本为空 | `G4_MISSING_EVIDENCE` |
| G5 关键系统不可用 | S07 / K006 | 最后 3 天无快照 | `G5_SYSTEM_UNAVAILABLE` |

统一返回 HTTP 200 + `ok=false` + `GUARDRAIL_STOPPED`；这是服务端硬拦截，不是模型判断。

## 5. 验收证据（可复跑）

```powershell
.\.venv\Scripts\python.exe -m pytest tests -q    # 28 passed
.\.venv\Scripts\python.exe scripts\smoke.py      # 21/21 PASS
```

`smoke.py` 覆盖：健康/就绪、七角色登录、admin 越权、R10 拦截、两种缺货分母、五类兜底、建议→提交→签核→重建、三跳溯源、审计脱敏与留存、数据就绪度。若服务未启动，脚本会自动拉起；结束后只清理自己拉起的进程。

## 6. 已知限制

- 本地模型不可用时建议走规则基线（`degraded=true`），不伪造模型输出。
- 向量检索不可用时降级 FTS5（中文二元组），页面会标明 `fts5`。
- `shelf_restock_hours` 恒为 `DATA_NOT_READY`，`turnover_days` 未签发恒为 `METRIC_BLOCKED`；这是特意保留的“不出数”演示，不是缺陷。
- 42 天窗口只能作护栏：测不出真实周转趋势与报废季节性。
- 数据为合成；金额、门店、人名均不对应真实企业。

## 7. 排障

| 现象 | 原因 | 处理 |
|---|---|---|
| 启动即退出，提示 `APP_JWT_SECRET` | `.env` 缺失或为空 | 从 `.env.example` 复制并填密钥 |
| `unable to open database file` | 不在仓库根执行，或未建库 | 回到仓库根重跑 `scripts/init_db.py` |
| `/api/ready` 503 | Ollama 未启动或模型未拉取 | 启动 Ollama，`ollama pull qwen3.5:9b`、`ollama pull nomic-embed-text` |
| 建议显示 `degraded` | 模型超时/不可用 | 看 `/api/ready` 的 `checks.llm`；功能仍可用 |
| 局域网打不开 | 防火墙或用了开发模式 | 用 `run_prod.bat`（0.0.0.0），放行 8000 端口 |
