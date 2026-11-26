# src · 模拟项目代码（MVP 已构建）

本目录是阶段 03 的可运行 MVP：FastAPI 服务 + 原生单页前端。数据为 8 店 × 50 SKU × 42 天合成数据，业务只到「待签核 → 人工签核」，没有下单接口。

## 结构

```
src/
├── app/
│   ├── main.py         应用工厂、统一响应、异常兜底、静态页挂载
│   ├── config.py       config.yaml + 环境变量（密钥/地址/端口/库路径不落字面量）
│   ├── errors.py       固定错误码与 {ok,data,error} 信封
│   ├── security.py     scrypt 口令 + PyJWT HS256
│   ├── db.py           SQLite（WAL + 外键 + 递归 CTE + FTS5）
│   ├── deps.py         三层权限：角色 → 数据域 → 动作
│   ├── llm/            Ollama 唯一调用点（think=false）与提示词
│   ├── agents/         diagnose / suggest / trace / guardrail / registry
│   ├── services/       metrics / graph / retrieval / replenishment / audit
│   └── routers/        auth / dashboard / metrics / graph / replenishment / admin / audit / health
└── web/                index.html + style.css + app.js（无 CDN、无构建）
```

## 启动

```powershell
.\.venv\Scripts\python.exe -m uvicorn src.app.main:app --host 127.0.0.1 --port 8000
```

完整步骤（建库、图谱、生产模式）见仓库根 [`README.md`](../README.md) 的「运行与部署」，演示动线见 [`DEMO.md`](../DEMO.md)。

## 必须遵守的约束

以下不是设计偏好，而是案例中已被明确记录的约束：

1. **只读优先。** 供应商下单接口尚未审批（f32），首期只导历史 CSV，不写回业务系统。
2. **不得绕过人工签核。** 建议最多到 `PENDING_SIGN`，签核/驳回必须由运营总监执行；admin 只能看（f44、f45）。
3. **关键原因必须是结构化字段。** 兜底命中、人工修正、签核人都要能重建（f14、f45）。
4. **接口失败要降级而不是中断。** 模型不可用走规则基线，向量不可用降级 FTS5，保留只读快照（f35）。
5. **不记录口令与非必要个人信息。** 审计写前过敏感字段白名单，留存 90 天（f45、R4/R5）。
6. **历史快照不得伪装成当前状态。** 超过一个周期的结果标注待刷新（f34）。
7. **口径未签发不出数。** 未签发指标返回 `METRIC_BLOCKED`，数据不足返回 `DATA_NOT_READY`，不填估算值（R10）。
