# cc-etl-lab

CC_Codex 二期（26.11）**数据工程**产品线：**抽取转换加载台**（FastAPI + Vue 3 SPA + SQLite）。

与 `cc-data-lab`（Flask + HTMX 采集控制台）错开：本线面向 **ETL 管道编排 / 抽取落库**，前端为 Vue 单页，暖色仓运风格。

## 领域

- **所属领域**：数据工程  
- **子方向**：抽取-转换-加载 / 数据采集与落库  

## 技术栈

| 层 | 选型 |
|----|------|
| 后端 | Python 3.12、FastAPI、Uvicorn、SQLite |
| 前端 | Vue 3（CDN Composition）、双栏管道台 |
| 风格 | 暖琥珀仓运底、圆角强调、等宽源地址 |
| 测试 | pytest + httpx |

## 基线能力

登录、管道列表、管道详情、执行抽取、执行落库。后续单轮题在此叠加增量水位、清洗口径、并发争用、跨日重算等。

## 增量：按日管道配额

- **四处联动**：配额看板、管道总览、落库详情、超额设置同改；看板与总览横排可读。
- **服务端记账**：配额按后台日历日结算，落库扣减写入 `quota_ledger`；`ETL_SETTLE_DAY` 可钉死结算日（测试用）。
- **超额路径**：只能选一种写进说明（`pool` 超额进候补池·次日再落 / `fail` 超额当场失败·配额已满），当日禁止改口；落库时路径偏离说明返回「超额路径写歪了」。
- **扣减规则**：抽取条数可大于配额，当日成功落库数不得超过配额；并发争抢最后名额只成一笔；管道未抽完不能落库。
- **候补池**：超额行入候补（`candidates`），隔日不自动落；过日期边界后点「候补继续」按新日配额重算。
- **可读性**：总览行显示配额、今日已落、候补条数、路径名称；详情可见候补顺序与配额抢占记录（`quota_events`）。

### 接口

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/quota/board` | 配额看板：结算日、合计、各管道横排 |
| POST | `/api/pipelines/{id}/quota` | 超额设置：`daily_quota` / `overflow_path` |
| POST | `/api/pipelines/{id}/load` | 落库；可带 `overflow_path`，偏离说明即拦 |
| POST | `/api/pipelines/{id}/candidates/continue` | 隔日候补按新日配额续落 |

## 本地

```powershell
cd claude-workspace\cc-etl-lab
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
pytest
python run.py
```

http://127.0.0.1:6000/login · **admin / 123456**

## Docker

```powershell
docker compose up -d --build
```

端口 **6000**。冒烟：`python ..\scripts\_smoke_lab_access.py --dir .`
