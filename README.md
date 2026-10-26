# OCI 免费 ARM 抢机（GitHub Actions 版）

Oracle Cloud Always Free 的 `VM.Standard.A1.Flex` 经常「Out of host capacity」。
这个仓库用 GitHub Actions 的定时任务不断重试 `LaunchInstance`，抢到就停。

- 每 5 分钟触发一次，`concurrency` 保证同一时间只有一个任务在跑，单个任务最多跑 ~9 分钟
- 抢到后后续运行会发现同名实例已存在，直接秒退
- 所有凭证都放在 **Repository Secrets** 里，代码本身不含任何密钥

## 需要的 Secrets

| Name | 说明 |
|---|---|
| `OCI_USER` | 用户 OCID |
| `OCI_FINGERPRINT` | API 密钥指纹 |
| `OCI_TENANCY` | 租户 OCID |
| `OCI_REGION` | 例如 `ap-tokyo-1` |
| `OCI_PRIVATE_KEY` | API 私钥 PEM 全文 |
| `OCI_COMPARTMENT_ID` | 隔间 OCID（一般就是租户 OCID） |
| `OCI_SUBNET_ID` | 公网子网 OCID |
| `OCI_SSH_PUBLIC_KEY` | 注入实例的 SSH 公钥 |

## 手动跑一次

Actions → 「OCI 免费 ARM 抢机」→ Run workflow。

## 注意

- 公开仓库的 Actions 分钟数免费不限量；私有仓库会消耗额度（本方案建议公开仓库，密钥在 Secrets 里不会暴露）
- 仓库 60 天没有任何提交时 GitHub 会停用定时任务，`keepalive` 工作流每月自动提交一次保活
- 免费额度：ARM 合计 2 OCPU / 12 GB、AMD 最多 2 台、块存储 200 GB。PAYG 账号超量会真实扣费
