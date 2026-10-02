# 洪灾安置家庭团聚与救助物资核对

管理洪灾撤离、安置容量、家庭团聚、敏感需求和救助物资。后端面向**夜班交接**：
下一班必须能说清一个家庭为何被拆分、各成员当时在哪里、最后依据哪些线索团聚。

## 参与方与事实

主要参与方包括省级救灾协调员、地方救援队、安置点工作人员、受灾家庭、医疗与社会服务人员。领域资料记录以下已经确认的事实：

- 泰国洪灾影响17府约57.4万户和180万人
- 灾害造成伤亡并需要持续救助
- 官方预计部分地区水位需要较长时间消退

## 后端组成（纯标准库，Python 3.11+，无外部依赖）

| 模块 | 职责 |
| --- | --- |
| `src/shelter/schema.py` | SQLite 结构与触发器：台账禁删、只追加表禁改、原始接收事实不可变 |
| `src/shelter/store.py` | 连接、WAL、外键与结构引导；对同一文件重开即“应用重启” |
| `src/shelter/service.py` | 领域服务 `Backend`：见下 |
| `src/shelter/scheduler.py` | 持久化调度：容量预警、失联匹配、药品补给、待确认转运 |
| `src/shelter/access.py` | 岗位 × 敏感类别访问矩阵，读取（含拒绝）全部留痕 |

### 覆盖的业务规则

- **社区/家庭/人员**：身份不明者先建 `unidentified` 临时档案；有自称身份为 `provisional`；核验通过为 `verified`。
- **合并而非覆盖**：线索确认后把重复档案指向保留档案，被合并档案整行保留，
  原始接收地点、时间、经办队伍、经办人继续可查（触发器禁止修改），历史移动与物资仍挂原档案、查询自动解析。
- **分岗隐私**：医疗资料仅医疗岗、儿童资料仅儿童保护岗、老人资料仅长者照护岗、
  无障碍需求同时对安置点工作人员开放；任何敏感读取在 `access_log` 留痕（含被拒绝的）。
- **撤离批次**：批次、车辆、队伍、登车/到达、未到（no_show）逐人记录。
- **安排联动**：转运、跨区调剂、出院、返家发生时，尚未执行的安排按规则取消（`cancelled`）
  或接续改派（旧条目标 `superseded`，新条目以 `follows_arrangement_id` 挂接），不丢失原计划。
- **容量**：当前在住由移动台账推导；满员/85%/无障碍床位预警去重，解除后可再次触发；
  超员默认拒绝，紧急时可显式 `allow_overflow` 放行并留痕。
- **物资守恒**：`stock_ledger` 只追加。发放 `issue`、退回 `return`、转交成对
  `transfer_out/transfer_in`、补发 `reissue`（必须挂原发放行）、主管修正
  `correction_in/out`。任何记录不得删除或修改。
- **寻亲团聚**：线索对在册人员打分（姓名/昵称/年龄层/语言/社区/双向家庭关系/队伍佐证），
  候选只提议不自动认定；接受候选后由主管/儿童保护岗确认团聚，决定与线索在
  `reunion_decisions`/`reunion_evidence` 互相留证。
- **回执幂等**：同编号且关键内容一致只处理一次（记 `duplicate`）；关键内容不一致不记账、
  入主管冲突队列并发 critical 预警；主管可 `accept_new`（台账冲抵修正）/`keep_existing`/`manual`。
- **可重启接续**：四类周期任务持久化在 `scheduled_jobs`，带幂等键；重启后 `run_due()`
  按原 `run_at` 节奏继续，失败任务保留待重试，不产生重复任务。
- **交接视图**：`family_handoff()` 汇总成员现状、`separation_summary()` 解释拆分原因、
  `family_timeline()` 给出接收→批次→移动→合并→团聚的完整时间线。

## 快速使用

```python
from datetime import datetime, timezone
from src.shelter import Backend, Scheduler, connect
from src.shelter.clock import utc_clock

conn = connect("shelter.db")          # 文件库，重启再开同一个文件
b = Backend(conn, utc_clock())
s = Scheduler(b)

med = b.register_operator("M1", "นพ.สมชาย", "medical", team="医疗队甲")
sh  = b.register_shelter("廊曼安置点", capacity_total=200)
pid = b.intake_person(received_location="寺庙", received_by_team="救援队一队",
                      received_by_operator="队员甲", shelter_id=sh)

s.ensure_default_recurring()          # 登记四类周期任务（重启调用安全）
s.run_due()                           # 执行到期任务
conn.commit()

print(b.family_handoff(family_id))    # 夜班交接视图
```

## 开发命令

运行测试：

```bash
python3 -m unittest discover -s tests -v
```

编译检查：

```bash
python3 -m compileall -q src tests
```

两条命令只读写仓库内文件（端到端测试使用临时数据库），不需要连接外部业务系统。
`contracts/context.schema.json` 描述领域资料结构，`fixtures/context.json` 提供不含真实身份信息的示例。
