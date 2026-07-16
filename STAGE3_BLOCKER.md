# 阶段 3 卡点与解决记录：下单页 UI 触发困境

> **状态（2026-07-15）**：卡点已解决，完整 dry-run 已进入真实确认页、写入乘车人并选择席别，最终提交按安全开关跳过。真实占座/排队尚未执行。
> **本文档**：保留原卡点的诊断链，并记录最终采用的解法与剩余验收项。

## 零、解决结果

### 最终方案

最初验证了查询页真实预订按钮使用的：

```javascript
window.submitOrderRequest(secretStr, departTime)
```

但页面函数在调用接口前只执行一次 `checkUser`。现场连续检测得到过 `[true, true, false, true, true]`，说明该检查在当前负载均衡环境中会偶发假阴性并弹出登录框。最终采用同一函数生成的官方请求参数，直接调用接口：

1. 打开 `leftTicket/init`，完成 UAM 会话刷新。
2. 将阶段 2 的 `secret_str`、日期、站名、单程和成人票参数 POST 到 `leftTicket/submitOrderRequest`。
3. 服务端返回 `status=true, data="0"` 后进入 `confirmPassenger/initDc`。
4. 在真实乘车人列表内勾选姓名；学生身份提示按当前 `ADULT` 流程选择成人票。
5. 为所有订单行设置目标席别，并在 `dry_run=true` 时停在“提交订单”之前。

这条路径仍复用浏览器会话与 12306 官方接口，但不再依赖查询结果表格渲染或不稳定的前置 `checkUser`。

### 同时发现的真实问题

原 `LoginManager.is_logged_in()` 通过 `.login-user` 判断登录，但该容器在未登录页面也存在。现场包裹页面 `jQuery.ajax` 后，官方 `/otn/login/checkUser` 返回 `data.flag=false`，证明此前的“复用本地会话成功”是误判；余票查询无需登录，所以阶段 2 仍会正常工作，掩盖了这个问题。

登录态现已改为调用页面同源 `login/checkUser` 接口。另发现 Chrome 用 `expires=-1` 表示会话 cookie，旧代码却把它作为过期时间注入，导致 `uamtk`、`JSESSIONID`、`tk` 立即失效。现在会话 cookie 不传 `expires`，并在复用时执行 `uamtk → uamauthclient` 握手恢复 OTN 会话。

### 已完成验证

- `python -m unittest discover -s tests -v`：11 项通过。
- `python -m compileall -q src tests`：通过。
- 过期 cookies 注入后，新登录检测正确返回 `False`；人工登录一次后可跨浏览器进程完成 UAM 恢复。
- 页面实测确认存在 `window.submitOrderRequest`，真实预订按钮最终也调用该函数。
- 本地拦截（未发送）原生函数生成的请求，确认 URL 为 `/otn/leftTicket/submitOrderRequest`，并包含正确的 `train_date`、`back_train_date`、`tour_flag=dc`、`purpose_codes=ADULT`、站名和 `_json_att` 占位；页面会自动解码传入的 `secret_str`。

### 现场验收结果

- 自动恢复 21 条 cookies，无需再次滑块登录。
- G309 二等座预订入口返回成功并进入真实 `confirmPassenger/initDc`。
- 正确限定到 `#normal_passenger_id`，避免误点顶部导航用户名。
- 学生身份乘车人按当前成人票流程处理，订单行成功写入姓名。
- `seatType_1` 成功设置为二等座代码 `O`。
- `dry_run=true` 在点击“提交订单”前停止，没有真实占座。
- dry-run 会检查 `passengerTicketStr`、`oldPassengerStr`、重复提交 token、`key_check_isChange`、`leftTicketStr` 和 `train_location` 的就绪状态，且不输出具体票串。
- 真实提交代码只点击可见的 `qr_submit_id`，并仅以进入 `payOrder/init` 作为成功；隐藏模板文字不再参与判定。

剩余未执行项只有 `dry_run=false` 后的真实提交、排队和待支付结果判定。

2026-07-16 尝试用次日车次复验新增的最终参数校验，但隔夜 passport 会话已过期且人工验证码等待超时，因此该次 live 复验未执行订单入口；配置文件未修改。

## 一、卡点现象

复用阶段 1 登录会话 + 阶段 2 已确认有票（G309 二等座），走到下单流程时：

```
已填入查询表单：北京(BJP)->桂林(GLZ) 日期=2026-07-16  实际DOM值=BJP|GLZ|2026-07-16
查询后结果表格总行数=0, 数据行=0
点预订后 URL 已变化：https://kyfw.12306.cn/otn/leftTicket/init?linktypeid=dc
找到提交按钮=True，当前URL=...leftTicket/init...
确认页判定：疑似未到确认页✗（URL不含confirmPassenger）
```

**症状**：DOM 里的电报码/日期都对了，但**查询结果表格 0 行**，页面 URL 始终停在 `leftTicket/init`，从未跳到 `confirmPassenger`。

## 二、当时的根因判断（已被后续诊断补充）

12306 查询页的"查询"按钮 click handler 有严格的前端校验：

1. 检查 `#fromStation` / `#toStation`（隐藏电报码域）是否有值
2. **同时**检查内部状态标志（如"用户是否手动从下拉列表选中过站点"、`window.check_from_station()` 等函数返回值）
3. 检查 `#fromStationText` 是否带 `class="error"`
4. 校验日期是否在放票范围内

我们能通过 JS 满足条件 1、3、4，但**条件 2 需要真实的用户下拉交互产生的内部状态**，`dispatchEvent(change/blur/input)` 也无法伪造该状态。

## 三、已排除的可能性（每一项都做过验证）

| 假设 | 验证方式 | 结论 |
|------|---------|------|
| DrissionPage 版本不兼容 | 从 4.1.1.4 升到 4.2.0b9 | ❌ 与卡点无关 |
| Chrome 150 不兼容 | 手动 `chrome --remote-debugging-port=9333` 成功 | ❌ 无关 |
| 高位调试端口被 EDR 拦截 | 改用 9331 后启动成功 | ✅ 已修，非本卡点 |
| Cookies setter 递归 bug | 改用 CDP `Network.setCookie` | ✅ 已修，非本卡点 |
| 走错页（停在 JSON 页） | 改走 `leftTicket/init` HTML 页 | ✅ 已修，非本卡点 |
| 表单值没进 DOM | 日志确认 `实际DOM值=BJP\|GLZ\|2026-07-16` | ❌ 值已进 DOM |
| run_js IIFE 导致 arguments 丢失 | 改成顶层函数表达式 | ✅ 已修，非本卡点 |
| 未派发 change 事件 | 加了 input/change/blur/keyup 派发 | ⚠️ 派发了，但查询仍 0 行 |
| 点错了预订按钮元素 | 表格 0 行，根本没预订按钮可点 | — |

## 四、历史方案评估

### 路线 A：入发房隔离方案（走接口，绕开页面 UI）

**思路**：阶段 2 的查询接口能拿到 `secret_str`（车次的下单凭证）。直接调用 12306 的下单接口 `otn/leftTicket/submitOrderRequest`，跳过页面 UI 交互，让浏览器直接接住返回的 `confirmPassenger/initDc` 跳转。

- **优点**：彻底绕开页面 JS 校验；逻辑清晰、每一步可控
- **风险**：需要逆向 `submitOrderRequest` 的完整参数（不止 secret_str，还有 back_train_date、tour_flag、purpose_codes、query_from_station_name、query_to_station_name 等），且可能需要 `_json_att` token；参数格式偶尔变动
- **工作量**：估计 4-8 小时（含实测调试）

### 路线 B：半自动方案（人工点一下查询按钮）

**思路**：脚本把行程填好、浏览器停在 `leftTicket/init` 页，日志提示用户"请手动点击'查询'按钮，然后回车"。用户配合一次点击，触发 12306 的内部状态，后续下单流程完全自动。

- **优点**：改动最小（几十行）；100% 稳定；避开一切前端校验
- **代价**：不是"完全无人值守"，抢票时需要用户守着；每次抢票循环都要点一次
- **工作量**：1 小时

### 路线 C：hack 方案（重写页面校验函数）

**思路**：加载 `leftTicket/init` 后，用 `page.run_js` 找出并**重写 12306 前端的校验函数**（如 `window.check_from_station = function(){return true;}`），让它总是返回 true。然后再点查询按钮。

- **优点**：仍是纯浏览器方案，不碰接口
- **风险**：需要知道 12306 内部函数名（会随版本变化）；需要用户先在 F12 Console 里执行 `keys(window)` 或类似操作，把可疑函数名报给我
- **工作量**：2-4 小时，含用户配合调研 + 迭代

## 五、我的推荐

如果只是**自用一次抢票**：**路线 B** 最快最稳，一小时可用。

如果打算做成**长期可复用的抢票工具**：**路线 A** 值得投入，脱离页面 UI 后长期维护成本反而更低（12306 页面 JS 改动比接口频繁）。

**不建议路线 C**：需要频繁调研内部函数名，是"技术上有趣但实用性差"的方案。

## 六、当前代码里保留的诊断能力（不用重写）

阶段 3 已实现的部分**全部保留、不需推翻**，路线 A/B/C 都可以复用：

- 完整登录会话（含 CDP cookies 注入）
- 查询模块（能拿到 secret_str）
- 下单骨架：`_open_booking` / `_select_passengers` / `_select_seat` / `_confirm_order`
- 干跑安全开关 `order.dry_run`
- 完整诊断链：URL 追踪、DOM 值验证、页面 HTML 转储、确认页判定
- 浏览器隔离与稳定启动逻辑

无论走 A/B/C 哪条路，"到确认页之后"的乘客勾选 / 席别选择 / 提交逻辑仍需按真实 HTML 校准选择器（这是阶段 3 收尾的必然工作），但会在跨过卡点之后进行。

## 七、决策记录

- 2026-07-15：阶段 3 骨架完成，实测卡在查询未生效
- 2026-07-15：暂停阶段 3，固化阶段 1/2 成果，本文档记录卡点
- 2026-07-15：发现 `.login-user` 导致登录态假阳性，改用官方 `login/checkUser`
- 2026-07-15：修复会话 cookie 过期元数据并加入 UAM 会话恢复
- 2026-07-15：绕过偶发假阴性的页面前置检查，官方预订入口返回成功
- 2026-07-15：真实确认页乘车人和席别选择完成，完整 dry-run 通过
- 待完成：经明确授权后验证真实提交、排队和待支付结果
