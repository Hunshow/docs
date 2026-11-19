# 东湖门诊中心 · 门诊预约与分诊协作智能体系统（MVP）

面向患者 / 分诊护士 / 科室医护 / 管理员四类角色，用智能体完成**预约核验、流量预判、动态分诊推荐、协同提醒**。
所有智能体输出都只是"建议"，必须人工确认后生效；系统不生成任何诊疗结论，数据全程本机保存。

---

## 1. 启动（Windows）

```powershell
cd C:\Users\Hunshow\WorkBuddy\2026-11-12-15-23-43\donghu-clinic-agents
C:\Users\Hunshow\.workbuddy\binaries\python\envs\default\Scripts\python.exe -m pip install -r requirements.txt   # 仅首次
C:\Users\Hunshow\.workbuddy\binaries\python\envs\default\Scripts\python.exe main.py
```

打开 <http://127.0.0.1:8310/> 自动跳转到演示界面（`/ui/`）。
接口文档：<http://127.0.0.1:8310/docs>。

## 2. 演示账号（登录页有一键按钮）

| 角色 | 账号 | 口令 | 能做什么 |
|---|---|---|---|
| 患者 | patient01 | patient-pwd-01 | 预约挂号、查看本人预约、查候诊进度 |
| 分诊/护士 | nurse01 | nurse-pwd-01 | 核验预约、看队列、生成分诊推荐并确认/驳回、发通知 |
| 科室医护 | doctor01 | doctor-pwd-01 | 看队列、看流量预警、反馈分诊异常 |
| 管理员 | admin01 | admin-pwd-01 | 看统计、调阅审计记录 |

## 3. 五分钟演示脚本

1. **患者**：登录 → 填预约 → 提交，列表里手机号/病历号已打码 → 查 `APT-002` 候诊位次。
2. **护士**：登录 → 核验 `APT-002`（看事实引用与资料版本）→ 换 `APT-003` 生成分诊推荐。
3. **关键卡点**：直接点"确认"会提示**必须填写人工理由**（质控留痕）→ 填理由后再确认，展示签核人、确认时间、资料版本。
4. **护士**：对 `APT-001`（医生钱有光，急诊暂停）核验 → 命中"排班未同步"，系统**拒绝自动推荐**并给出转人工三要素。
5. **科室医护**：看队列（迟到/加号有标注）→ 跑一次流量预判 → 提交异常反馈（状态 `PENDING_REVIEW`，不改任何分诊结果）。
6. **管理员**：看统计（待人工确认 / 已确认 / 已驳回）→ 调阅审计，能看到 `CONFIRM_RECOMMEND`、`FEEDBACK_ANOMALY`、`DENIED`（越权尝试）等记录。

## 4. 自测

```powershell
python run_verify.py     # 阶段一 32 项：鉴权、脱敏、审计、人工确认闭环
python run_mvp_check.py  # MVP 30 项：界面可达 + 四角色主链路 + 新增反馈接口 + 权限卡点
```

## 5. 工程结构

```
app/
  api/routes.py      接口 + RBAC 卡点 + 人工确认链路
  security/          脱敏 / 审计留痕 / HMAC 令牌与权限矩阵
  hermes/            Hermes 引擎（感知-思考-行动）+ 四个智能体 + 只读工具
  rag/               RAG 检索（BM25+jieba）+ 知识图谱
  services/store.py  模拟预约平台 / HIS / 叫号系统只读视图
web/                 MVP 演示界面（原生 HTML/CSS/JS，无外部依赖）
data/kb/             制度知识库与三元组
data/audit/          审计留痕 JSONL
config.yaml          唯一配置入口（密钥、模型地址、脱敏规则、演示账号）
```

## 6. 红线约束的落点

- **不生成诊疗结论**：智能体只输出核验结论、次序解释、流量提示；`triage_advisor` 恒返回 `requires_human_confirm=true`。
- **所有推荐人工审核**：推荐先入 `PENDING_HUMAN` 暂存，`/confirm` 接口强制 `note` 非空才生效，并记录签核人与时间。
- **敏感信息脱敏**：所有出口经 `mask_record/mask_list`，姓名、手机号、病历号打码后才返回。
- **留痕审计**：每次建议、确认、驳回、越权尝试、审计调阅本身都写 `data/audit/audit.jsonl`，字段含业务编号、资料版本、事实引用、人工改动、签核人。
- **数据本地保存**：监听 `127.0.0.1`，模型走本地 Ollama，前端不引用任何 CDN；Ollama 不在位时自动降级为规则推理（`mode=rules`）。

## 7. 线上部署（已上线）

演示实例：<https://clinic-triage-agent-v2-32190.app.workbuddy.host/?sharecode=FLYICY2G52>

> 2026-11-19 更新：修复"我的预约与预约信息无关联"问题（预约记录绑定登录账号归属），演示种子数据医生/科室多样化。

部署方式与本地运行共用同一份代码，区别只有启动时是否注入 `PORT` 环境变量：

- 平台注入 `PORT` → 应用绑定 `0.0.0.0:$PORT`（被反向代理访问）
- 无 `PORT` → 应用仍只绑 `127.0.0.1:8310`（本地演示，不对外暴露）

### 网关鉴权注意（踩过的坑，改代码前必读）

线上网关会**把客户端送来的 `Authorization` 头整个替换成它自己的 JWT**（实测收到的头形如
`Bearer <36>.<298>.<43>`，长度从 194 变成 386），原始令牌直接丢失，表现为“登录成功、
所有业务接口 401”。因此：

- 前端 `web/app.js` 现在**同时**发送 `X-Auth-Token`（自定义头，网关原样透传）和 `Authorization`（本地直连用）；
- 后端 `get_identity` 支持三条通道：`Authorization` → `X-Auth-Token` → `app_token` Cookie，
  且会从网关 JWT 载荷的 `token` 字段里尝试取回原始令牌；
- 无论走哪条通道，都必须过同一套 HMAC 验签，不存在“换通道就免检”。

诊断接口：`GET /api/auth/whoami`（仅 `config.yaml` 里 `app.debug: true` 时可用），
只回显 header/cookie 的**名字**、长度与脱敏形状，不回显任何凭证值。

## 8. 已知边界（MVP 范围外）

- 数据是内存 + JSON 文件，多进程部署会各存一份，接真实 HIS 前需换数据库。
- 患者身份按展示名匹配（演示口径），接统一身份认证后需改为病历号绑定。
- 演示口令与签名密钥明文在 `config.yaml`，生产必须走环境变量/密钥管理。
- 排队叫号、排班写回等写操作未实现（红线：系统不改队列与诊疗数据）。
- **线上实例仅供演示**：分享链接在公网可访问，与“数据本地保存”红线冲突，正式环境必须部署在院内网；演示时不要录入任何真实患者信息。
