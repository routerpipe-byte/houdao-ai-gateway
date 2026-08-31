# Tesla 西班牙 Model Y 现车提醒

本任务每 10 分钟检查一次西班牙 Model Y 库存，并在发现新的车辆编号时创建一个分配给 `routerpipe-byte` 的 GitHub issue。GitHub 会按照该账号的通知设置，将 issue 通知发送到账号绑定邮箱。

## 监控目标

Tesla 官方搜索页：

`https://www.tesla.com/es_ES/inventory/new/my?arrangeby=plh&zip=46183&PaymentType=cash`

Tesla 会拦截 GitHub 托管运行器的访问，因此任务使用 Teslastats 对外公开的 **Spain + Model Y** 库存列表作为变化触发源。提醒中始终附带 Tesla 官方搜索页，最终库存状态和价格必须以 Tesla 官方页面为准。

## 隐私与凭据

任务不保存 Gmail 地址、Gmail 密码、Google OAuth token 或 SMTP 凭据。提醒通过 GitHub issue 通知机制发送到 GitHub 账号绑定的邮箱。

## 状态与去重

`state.json` 记录已经见过的车辆编号及其公开库存字段。库存没有变化时不会提交状态，也不会重复提醒。同一批车辆还会在 issue 正文中写入隐藏标记，避免重试时产生重复 issue。

## 测试

Pull request 运行采用 `DRY_RUN=true`：会真实读取并解析库存源，但不会创建提醒，也不会修改状态文件。合并到 `main` 后会立即执行一次正式检查，此后按 10 分钟计划运行。
