---
AIGC:
  ContentProducer: '001191110102MAD55U9H0F10002'
  ContentPropagator: '001191110102MAD55U9H0F10002'
  Label: '1'
  ProduceID: '76eae816-deb9-4d42-a31c-e356a1218ed1'
  PropagateID: '76eae816-deb9-4d42-a31c-e356a1218ed1'
  ReservedCode1: 'ada37302-c817-48d6-b714-45f53506c8d5'
  ReservedCode2: 'ada37302-c817-48d6-b714-45f53506c8d5'
---

# IHG 积分房监控（GitHub Actions 版）

定时查询 IHG 官网积分房（Reward Night / 奖励晚）是否可订，**一有房间立刻发邮件提醒**。

- 走 IHG 官网搜索页同款公开接口，无需登录 IHG 账号
- 纯 Python 标准库，零依赖，跑在 GitHub Actions 免费的公共仓库额度上（Public repo 不消耗每月 2000 分钟额度）
- 状态翻转才发邮件（有→无、无→有），不会每小时轰炸收件箱
- 邮件经 [Resend](https://resend.com) 发送（免费版 100 封/天、3000 封/月，足够）

## 一、文件结构

```
ihg-points-watcher/
├── check_ihg.py                    # 主脚本（查房 + 发邮件）
├── .github/workflows/check.yml     # GitHub Actions 定时任务
└── README.md
```

## 二、部署步骤（约 5 分钟）

### 1. 建仓库

GitHub 上新建一个 **Public** 仓库（Private 也可以跑，但会消耗免费额度），把本目录三个文件原样上传：

- `check_ihg.py` 放仓库根目录
- `check.yml` 必须放在 `.github/workflows/` 下（连文件夹一起传）

网页上传方法：仓库页 "Add file → Upload files"，`.github` 文件夹可以先新建一个同名文件占位，或直接用命令行：

```bash
git clone https://github.com/<你的用户名>/<仓库名>.git
cd <仓库名>
# 把本目录文件复制进来，保持相对路径不变
git add .
git commit -m "init: ihg points watcher"
git push
```

### 2. 改酒店和日期

编辑 `.github/workflows/check.yml` 里 `Run check` 这一步的三个变量：

```yaml
IHG_HOTEL_CODES: "OKJJA"        # 换成你的酒店代码，多个用逗号: "OKJJA,OSAKA"
IHG_CHECKIN: "2026-10-03"       # 入住日期
IHG_CHECKOUT: "2026-10-04"      # 退房日期
```

**酒店代码怎么找**：打开 IHG 官网该酒店的详情页，看浏览器地址栏，例如

```
https://www.ihg.com.cn/hotels/cn/zh/okayama/OKJJA/hoteldetail
                                          ↑这一段就是代码
```

### 3. 配置 Resend 邮件

1. 注册 [resend.com](https://resend.com)（免费），到 API Keys 页面创建一个 Key（`re_` 开头）
2. GitHub 仓库 → **Settings → Secrets and variables → Actions → New repository secret**，添加两个：

   | Secret 名称 | 值 |
   |---|---|
   | `RESEND_API_KEY` | 你的 Resend Key（如 `re_xxxxxxxx`） |
   | `NOTIFY_EMAIL` | 收提醒的邮箱地址 |

3. 免费版 Resend 未验证域名时只能用 `onboarding@resend.dev` 发信（脚本已默认如此，无需配置）。想在收件箱里更规范，可在 Resend 验证自己的域名后，加一个 Secret `RESEND_FROM`（或在 workflow env 里加）。

### 4. 试跑一次

仓库 → **Actions** 标签页 → 左侧选 "IHG Points Watcher" → 右侧 "Run workflow" 手动触发。

点进运行记录可以看到日志：

```
[...] 开始检查: 酒店=['OKJJA'], 入住=2026-10-03, 退房=2026-10-04
[...] OKJJA: 积分房(Reward Night) isAvailable = False
[...] 状态无变化，不发送邮件。
```

## 三、工作原理

脚本请求的接口与 IHG 官网搜索页完全相同：

```
POST https://apis.ihg.com.cn/availability/v3/hotels/offers?fieldset=summary,summary.rateRanges
```

无需登录 IHG 账号。返回的 JSON 中，酒店级字段直接给出积分房状态和价格（2026-09 实测）：

**有积分房时**（示例：OKJJA 冈山皇冠假日 2026-11-03）：

```json
{
  "rewardNightAvailable": true,
  "lowestPointsOnlyCost":     { "points": 24000 },
  "lowestPointsAndCashCost":  { "points": 9000, "cash": 89.0 }
}
```

**没积分房时**：`rewardNightAvailable` 与价格字段直接缺失，奖励晚价目条目（rate code `IVANI`）显式标记 `isAvailable: false`。

脚本按此判断并提取价格，邮件里会直接告诉你「24,000 分/晚」以及可选的「积分+现金」组合价。

## 四、状态记忆与去重

- 每次运行把结果存进 Actions cache（`.status/ihg_status.json`）
- 下次运行对比：**无→有** 发"有积分房了"，**有→无** 发"积分房没了"，不变则静默
- 首次运行只记录基线、不发邮件
- Actions cache 若被回收（约 7 天不使用才会），最多多收一封邮件，不影响功能

## 五、调整频率

`check.yml` 中的 cron 表达式（UTC 时间，比北京时间慢 8 小时）：

```yaml
- cron: "0 * * * *"     # 默认：每小时整点
- cron: "0 */2 * * *"   # 每 2 小时
- cron: "0 */6 * * *"   # 每 6 小时（省额度）
```

注意：GitHub Actions 定时任务高峰期可能延迟 5~30 分钟，属正常现象。

## 六、常见问题

| 问题 | 说明 |
|---|---|
| 查询报"酒店代码无效" | 酒店代码写错了，回到详情页 URL 核对（会在 1 秒内快速报错，不重试）|
| 邮件没收到 | Actions 日志里看 "提醒邮件已发送" 是否出现；未配 Secret 时只打印预览不发信 |
| 想换日期/酒店后立刻验证 | 先手动 Run workflow 一次；改配置不会清空旧状态，如需重置可删除 Actions cache（仓库 Settings → Caches）|
| 积分价是多少 | 邮件里已直接给出（纯积分/积分+现金两种），以下单页为准 |
| 接口失效了怎么办 | IHG 改版可能导致字段变化，届时用浏览器 F12 → Network 重新抓一次 `availability` 请求，更新 `check_ihg.py` 里的解析逻辑即可 |

## 七、合规提示

仅查询公开房价数据、低频访问（每小时 1 次），对 IHG 服务器几乎无压力；请勿把频率调到分钟级，也请勿用于倒卖囤房等用途。

> AI生成