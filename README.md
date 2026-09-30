# 酒店迷你吧补货与离店结算平台

一套面向客房迷你吧的运营平台：维护客房、商品、陈列上限、批次效期、入住订单、每日盘点、补货车、
消费确认、免单原因与退房冻结。所有客人与账单均为**匿名演示数据**。

零第三方运行时依赖，仅使用 Node.js ≥ 22 内置的 `node:sqlite`。

## 一条本地启动命令

```bash
npm start
```

首次启动若数据库不存在，会自动加载在住、待退、争议三类样例，随后在
**http://localhost:3000** 提供前台界面与 JSON API。

- 重新生成样例：`npm run seed`
- 运行测试：`npm test`
- 自定义端口 / 数据库：`PORT=8080 MINIBAR_DB=/tmp/mb.db npm start`

## 内置演示账号（左下角切换身份，体现楼层权限）

| 身份 | 范围 | 能做 |
| --- | --- | --- |
| 周经理 `stf_mgr` | 全部楼层 | 入住、入库、免单、冲销、所有复核 |
| 前台·林 `stf_fd3` | 3 层 | 本层消费确认、冻结、结算 |
| 前台·许 `stf_fd4` | 4 层 | 本层消费确认、冻结、结算 |
| 服务员·阿桂 `stf_att3` | 3 层 | 本层盘点、补货提交 |
| 服务员·阿禾 `stf_att4` | 4 层 | 本层盘点、补货提交 |

## 样例场景

- **301（在住）**：水含临期旧批 + 新批，已盘点消费 1 瓶，演示 FIFO 命中临期批次并补货。
- **302（在住）**：入住超 24 小时未盘点，触发「未盘提醒」。
- **401（待退）**：已消费并确认、已退房冻结（最后一次盘点锁定），等待前台确认账单并结算。
- **402（争议）**：客人否认啤酒消费，已开立「客人否认」复核工单。
- 补货车含**已过期**啤酒与**包装破损**坚果（后者带包装照片），只能报损不能上架。

## 核心业务规则

- **入住前快照**：办理入住时生成 v0 房内库存基线盘点。
- **盘点减少量 → 待确认消费**：实盘少于应有数即按批次 FIFO 核销并生成待确认消费；
  同时可从补货车按 FIFO（效期最早优先）转入新批次补足陈列上限。
- **版本化盘点**：同一客房盘点版本单调递增，提交须带 `baseVersion`；旧版本提交返回
  `409 version_conflict`，不得覆盖新快照。
- **离线重传**：相同 `clientGuid` 幂等去重，不重复入账。
- **批次 FIFO**：消费与补货都优先效期最早批次；已过期或包装破损批次只可报损，
  即使状态标志未更新，只要效期已过也不会再上架。
- **退房冻结**：冻结锁住最后一次盘点（`locked`），冻结后拒绝普通盘点，补录须走
  「退房后补录」复核；前台确认账单后经模拟结算网关返回参考号并关闭订单。
- **冲销 / 免单**：必须经理权限且留存原因；冲销为负向账单并按幂等键去重，重复冲销不重复扣减。

### 六类复核流程

`guest_deny`（客人否认）、`cross_room`（跨房放错）、`near_expiry`（批次临期）、
`cart_shortage`（补货车短缺）、`offline_reupload`（离线重传统重）、
`post_checkout`（退房后补录）各自进入不同工单处理路径。

## 交付页面

楼层房态（按楼层紧凑列表）、房内盘点（**商品图片 / 当前数 / 补货数**固定三列）、
补货车清单、消费复核（争议按**账单时间线**展开）、效期报损、退房结算。
配色为象牙白 / 藏青 / 香槟金；金额使用等宽数字对齐小数位，账单页提供打印友好样式。

## 存储与外部边界

- SQLite 保存库存版本、批次与账单：`data/minibar.db`（WAL）。
- 包装照片存入本地目录 `photos/`，经 `POST /api/photos`（原始字节）接收，`GET /photos/<file>` 访问。
- 进程内任务 `src/tasks/reminders.js` 周期扫描临期批次与未盘客房，写入 `notifications`。
- 模拟适配器 `src/adapters/pms.js` 返回房态码与结算参考号，无真实外部调用。

## 主要 API

| 方法 & 路径 | 说明 |
| --- | --- |
| `GET /api/floorboard` | 楼层房态（按身份过滤楼层） |
| `GET /api/rooms/:id/count-sheet` | 房内盘点视图（基线版本、当前数、建议补货） |
| `POST /api/rooms/:id/counts` | 提交盘点（`baseVersion`、`lines`、可选 `clientGuid`） |
| `GET /api/cart` / `POST /api/batches/receive` | 补货车清单 / 批次入库（经理） |
| `GET /api/consumptions` | 待确认/全部消费 |
| `POST /api/consumptions/:id/confirm` | 确认计费 |
| `POST /api/consumptions/:id/comp` | 免单（经理 + 原因） |
| `POST /api/consumptions/:id/reverse` | 冲销（经理 + 原因，幂等） |
| `GET|POST /api/reviews`、`POST /api/reviews/:id/resolve` | 复核工单 |
| `GET /api/batches/near-expiry` / `GET|POST /api/write-offs` | 效期与报损 |
| `POST /api/orders/:id/freeze` / `POST /api/orders/:id/settle` | 退房冻结 / 确认结算 |
| `GET /api/orders/:id/bill` | 账单时间线与合计 |
| `POST /api/photos` | 上传包装照片（`X-Filename` 头 + 原始字节） |

所有请求通过 `X-Staff-Id` 头选择身份（演示用，非真实鉴权）。

## 测试

`npm test`（node:test，内存数据库）覆盖：

- 盘点版本冲突与旧版本不得覆盖、离线重传幂等
- 批次先进先出核销与补货、过期/破损禁止上架
- 退房冻结锁定盘点、结算关闭
- 账单冲销幂等、经理权限与原因留存、退房后补录幂等
- 楼层权限越层 403、HTTP 层免单/冲销授权

## 目录结构

```
src/
  db.js                 SQLite schema
  util.js               事务/审计/金额等工具
  domain/               staff / inventory / stays / billing / reviews
  adapters/pms.js       模拟房态与结算适配器
  tasks/reminders.js    进程内临期与未盘提醒
  seed.js               样例数据
  server.js             零依赖 HTTP + JSON API + 静态站
public/                 前台（index.html / styles.css / app.js / img）
photos/                 包装照片落地目录
test/                   node:test 测试
```
