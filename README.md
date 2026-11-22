# 物业停电日报核算 API

FastAPI + SQLite 实现的按 UTC 日核算服务：登记楼栋、全天带电电源点、双向支路及支路导通/断开时段，
生成“已确认停电 / 待核实”的楼栋时段日报，并支持凭当前版号对单条支路做整日补正。

## 规则

- **时间**：一律按 UTC 日 `[00:00:00Z, 次日 00:00:00Z)`，时段精确到秒、左闭右开。
  持久化使用整数 epoch 秒，响应统一输出 `YYYY-MM-DDTHH:MM:SSZ`。
- **支路**：双向。每条支路的当日记录是一组 `on`/`off` 时段；同一支路内时段不得重叠，
  未覆盖的空档视为 **未知**（unknown）。登记/补正时段的起止必须落在当日范围内。
- **电源**：登记为 `power_sources` 的节点视为全天带电。
- **判定**（每个原子时间片内）：
  1. 只走已知 `on` 支路，能从楼栋到达任一电源 → **有电**；
  2. 否则把未知支路也当作 `on`，仍到不了电源 → **已确认停电**；
  3. 否则（乐观可达、确定不可达）→ **待核实**。
- **合并**：同一楼栋同类时段在结果中已合并为不重叠、按时间升序的区间；有电时段不返回。
- **版号**：登记生成 v1。补正成功后原子地写入新支路记录并生成 v(n+1)，旧的日报版本永久保留、
  仍可通过 `GET /days/{day}/report?version=n` 读取。补正失败（版本过期、校验失败、支路不存在、
  核算异常）不改变现行支路记录，也不产生新版本，原日报继续提供。
- **并发**：所有写操作在 SQLite `BEGIN IMMEDIATE` 事务内完成；同版号并发补正最多一个成功，
  其余返回 409 `version_conflict`。

## 核算性能

真实日报里绝大多数支路整日只有一个状态，只有少数支路频繁切换。核算器因此把支路分两步处理：

1. 把每条支路压缩成连续状态区间。只有一个区间的支路是**常态支路**，其余是**切换支路**。
2. 常态 `on` 支路（乐观图里还包括常态 `unknown` 支路）先用并查集收缩成连通分量，
   这一步对每个请求只做一次，就是可复用的“稳定支路连通信息”。
3. 原子时间片的边界只来自切换支路。每个时间片只在收缩后的分量图上、用切换支路的当前边做
   一次很小的可达性搜索，从不逐秒展开拓扑，也不会在每个切换点重扫全部支路。

确定图（只走已知 `on`）和乐观图（也允许走记录空档）分别收缩、分别搜索，因此
“并联支路 / 同秒切换 / 空档转为断开”都按原规则逐片判定。

耗时对比（同一份合成日报，`scripts/benchmark.py`，本机 Python 3.11，单次运行、耗时随机器波动）：

| 常态支路 | 切换支路 | 旧实现 | 优化后 | 加速 | 结果一致 |
| ---: | ---: | ---: | ---: | ---: | --- |
| 200 | 2 | 82.0 ms | 3.7 ms | 21.9x | 是 |
| 1000 | 3 | 510.9 ms | 9.3 ms | 55.0x | 是 |
| 3000 | 3 | 1683.8 ms | 22.3 ms | 75.5x | 是 |
| 5000 | 2 | 2841.5 ms | 37.6 ms | 75.6x | 是 |

`tests/reference_accounting.py` 保留了旧的全支路逐片算法，`tests/test_accounting_optimised.py`
用它做随机对拍，并用 `diagnostics` 计数断言“每片只检查切换支路”：
`branch_state_checks == atomic_intervals * switching_branches`。

```bash
python scripts/benchmark.py
```

## 运行（Docker Compose）

```bash
docker compose up --build -d
curl -s http://127.0.0.1:8000/health
# 交互式文档：http://127.0.0.1:8000/docs
```

数据保存在命名卷 `outage-data`（容器内 `/data/outage.db`）。

停止并保留数据：`docker compose down`；连数据一起清理：`docker compose down -v`。

## 演示请求

```bash
DAY=2026-11-21
BASE=http://127.0.0.1:8000

# 1) 登记：L1 在 08:00-10:00 是未知空档；L2 全天断开
curl -sS -X POST "$BASE/days/$DAY/topology" -H 'Content-Type: application/json' -d '{
  "buildings": [{"code":"A1","name":"1号楼"},{"code":"A2","name":"2号楼"}],
  "power_sources": [{"code":"P1","name":"主配电房"}],
  "branches": [
    {"code":"L1","name":"A1 主干","node_a":"P1","node_b":"A1","segments":[
      {"start":"2026-11-21T00:00:00Z","end":"2026-11-21T08:00:00Z","state":"on"},
      {"start":"2026-11-21T10:00:00Z","end":"2026-11-22T00:00:00Z","state":"on"}]},
    {"code":"L2","name":"A2 主干","node_a":"P1","node_b":"A2","segments":[
      {"start":"2026-11-21T00:00:00Z","end":"2026-11-22T00:00:00Z","state":"off"}]}
  ]
}'

# 2) 读取当前日报
curl -sS "$BASE/days/$DAY/report"

# 3) 补正 L2：凭 v1 整日替换（00:00-06:00 off，06:00-次日 on），生成 v2
curl -sS -X POST "$BASE/days/$DAY/branches/L2/corrections" -H 'Content-Type: application/json' -d '{
  "expected_version": 1,
  "segments": [
    {"start":"2026-11-21T00:00:00Z","end":"2026-11-21T06:00:00Z","state":"off"},
    {"start":"2026-11-21T06:00:00Z","end":"2026-11-22T00:00:00Z","state":"on"}]
}'

# 4) 旧版仍然可读
curl -sS "$BASE/days/$DAY/report?version=1"

# 5) 版本列表 / 再次用过期版号补正 → 409
curl -sS "$BASE/days/$DAY/versions"
curl -sS -X POST "$BASE/days/$DAY/branches/L2/corrections" -H 'Content-Type: application/json' -d '{
  "expected_version": 1, "segments": []
}'
```

预期：第 1 步 v1 中 A1 只有 `08:00-10:00` 待核实、无确认停电；A2 全天确认停电。
第 3 步 v2 中 A2 的确认停电收缩为 `00:00-06:00`。第 5 步返回
`409 {"error":{"code":"version_conflict", ...}}`，现行记录仍是 v2。

也可以直接跑脚本（需已启动服务）：

```bash
python scripts/demo.py http://127.0.0.1:8000
# 或本地一键：启动 → 演示 → 退出
python -m pip install -r requirements.txt && ./scripts/demo.sh
```

## 接口一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/days/{day}/topology` | 登记该 UTC 日楼栋/电源/支路，生成 v1（同日重复登记 → 409） |
| `GET` | `/days/{day}/topology` | 查看已登记拓扑及支路当前时段 |
| `GET` | `/days/{day}/report` | 当前版日报：每楼栋合并后的 `outages` / `pending` 与 `version` |
| `GET` | `/days/{day}/report?version=n` | 指定历史版本 |
| `GET` | `/days/{day}/versions` | 版本号列表与 `current` 标记 |
| `GET` | `/days/{day}/summary` | 便捷汇总：确认停电/待核实楼栋代码 |
| `POST` | `/days/{day}/branches/{code}/corrections` | 凭 `expected_version` 整日替换一条支路并生成新版 |
| `GET` | `/health` | 健康检查 |

错误响应统一为 `{"error": {"code": "...", "message": "...", ...}}`；常见 code：
`validation_error`、`invalid_day`、`invalid_timestamp`、`segment_out_of_day`、
`overlapping_segments`、`day_already_registered`、`version_conflict`、`branch_not_found`、
`report_computation_failed`。

`POST .../corrections` 成功时响应头带 `X-Report-Version`。

## 本地开发与测试

```bash
python -m pip install -r requirements-dev.txt
python -m pytest
```

测试覆盖：判定规则（未知→待核实、断开→停电、空洞、并联/环路、双向遍历、同秒切换、
空档转为断开）、与逐秒暴力实现及旧全支路实现的随机对拍、常态支路多/切换支路少的耗时对比、
登记校验、补正成功/过期/非法/核算失败回滚、旧版本可读，
以及多线程同版并发补正只成功一次。

## 数据模型

| 表 | 作用 |
| --- | --- |
| `topology_day` | 已登记的 UTC 日 |
| `node` | 当日的楼栋 / 电源点 |
| `branch` | 支路（双向，两端节点） |
| `branch_segment` | 支路时段：`start_sec`、`end_sec`、`state` |
| `report` | 日报版本（`day`, `version`, `generated_at`） |
| `report_building` | 版本内每楼栋的 `outages_json` / `pending_json` |

旧日报行一经写入不再修改，因此“重算时可读旧日报”是天然成立的。
