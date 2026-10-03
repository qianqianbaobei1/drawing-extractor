# 电柜智核 · 配电箱智能工作台 (CabinetCore AI)

上传配电箱 / 配电柜系统图（支持 PDF、AutoCAD DWG、DXF），AI 视觉与 CAD 矢量双核自动提取全部回路与元器件信息，在工业级工作台里进行图纸联动核对、电气合规校验、AI 助手问答，最后导出工业标准 Excel 报价四联表与跨箱体采购 BOM。

## 目录结构

```
drawing-extractor/
├── backend/
│   ├── app.py                 # FastAPI 主程序：上传→渲染→提取→组装→核对→Excel→问答
│   ├── store.py               # 项目 / 导出历史 / 设置的 JSON 落盘
│   ├── OUTPUT_CONTRACT.md     # 事实输出、定位坐标与组装规则
│   ├── stability_test.py      # 同图多次调用结果比较
│   ├── extractor/
│   │   ├── schema.py          # 模型事实与最终清单的两层数据模型（含 bbox / 存疑来源）
│   │   ├── assemble.py        # 确定性排序与数量汇总
│   │   ├── checker.py         # 引用与数量交叉核对
│   │   ├── assistant.py       # AI 助手问答：结构化动作 + 修改校验
│   │   ├── render.py          # PDF 按页渲染为图片
│   │   ├── vision.py          # 视觉模型调用（OpenAI 兼容接口）
│   │   └── excel.py           # Excel 报价清单生成器（含变更记录 sheet、模板）
│   ├── prompts/
│   │   ├── extract.txt        # 识图提取提示词
│   │   └── assistant.txt      # 助手问答提示词
│   ├── data/                  # projects.json / history.json / settings.json
│   ├── work/                  # 任务产物：<job_id>.pdf / .pageN.png / .xlsx / .json
│   └── tests/                 # 结构与业务回归测试
├── frontend/
│   ├── index.html             # 单页前端：项目/上传/工作台/历史/设置
│   ├── app.css                # 视觉基准（直角、#2E5CE6 主色、13px 正文）
│   └── app.js                 # 全部逻辑，直接调后端接口，无内置假数据
├── .env.example
└── run.sh                     # 一键启动
```

## 工作流程

```
上传 PDF → 按页渲染概览图（长边约 2400px，与模型有效分辨率对齐）
→ 大图（长边 >500mm）再切成带重叠的块，每块单独高分辨率渲染
→ 逐张调用视觉模型（不再把所有页塞进一个请求），坐标折回整页并写上页码
→ Pydantic 校验 → 跨块去重 / 跨页拼接 → 代码组装与交叉核对 → 生成 Excel
→ 前端在图纸上按页码定位、核对存疑项、改回路/箱体/非回路设备、问 AI → 导出
```

字迹不清的内容不会被编造，而是进入“存疑项”，在前端和 Excel 中标出请人工核对。
组装器对同一份已校验事实给出相同结果；识别准确率取决于模型，不能仅凭 temperature=0 保证。

### 识别链路的三条经验规则

1. **拆不开的写法不能丢。** `MCB 1P+N`、`QF1+QF2`、`DZ47LE-63/2P` 这类既没有已知前缀、
   数量也不明确的写法，一律按原文留一行并标记“数量待人工确认”。直接剔除汇总表会让报价少算钱，
   留一行标记过的数据只是要人多看一眼。
2. **整页大图堆像素是白费的。** 模型服务端会把输入图缩到固定尺寸，A1 整页出 6600px 也是被缩回去。
   所以概览图压到模型真能看清的尺寸，小字靠分块放大解决。
3. **不要假设 PDF 有文本层。** 国内出图普遍把文字转曲，实测项目里的 2SAL3/2SAL2 都是 0 个词。
   想评估混合流水线值不值得做，先在真实图纸上跑 `python -m extractor.render 图纸.pdf`。

## 快速开始

```bash
cd drawing-extractor
cp .env.example .env        # 填写 VISION_API_KEY
./run.sh                    # 自动建虚拟环境、装依赖、启动服务
# 浏览器打开 http://localhost:8000
```

### 环境变量（.env）

| 变量 | 说明 |
|---|---|
| `VISION_API_KEY` | 视觉模型 API Key（**必填**） |
| `VISION_BASE_URL` | OpenAI 兼容接口地址 |
| `VISION_MODEL` | 支持图像输入与 JSON 输出的模型 |
| `ASSISTANT_*` | 助手问答单独配置；不填则复用 `VISION_*` |
| `VISION_TEMPERATURE` | 识图温度，默认 0 |

也可以不改 `.env`，直接在页面「设置」里填。设置优先于环境变量，API Key 只回传
"是否已配置"，不会把明文发给前端。

## 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/jobs` | 上传 PDF（可带 `project`），后台提取 |
| GET | `/api/jobs` | 全部任务（项目页 / 历史页共用） |
| GET | `/api/jobs/{id}` | 任务状态、清单数据、变更记录 |
| GET | `/api/jobs/{id}/page/{n}` | 图纸第 n 页 PNG |
| PUT | `/api/jobs/{id}/data` | 保存清单与修改留痕，重算汇总并重建 Excel |
| POST | `/api/jobs/{id}/chat` | AI 助手问答，可返回切页签 / 定位 / 改参数 |
| POST | `/api/jobs/{id}/revert` | 撤销一条修改记录 |
| GET | `/api/jobs/{id}/excel` | 下载 Excel，并记入导出历史 |
| GET/POST | `/api/projects` | 项目列表 / 新建项目 |
| GET | `/api/history` | 导出历史 |
| GET/PUT | `/api/settings` | 运行设置 |
| GET | `/api/health` | 健康检查与模型配置状态 |

## 三条设计约定

1. **回路、箱体、非回路设备都可编辑。** 保存时元器件汇总与待核对告警从这三类事实重新推导，
   改了断路器或箱体台数不会留下对不上的旧汇总；模型漏了浪涌保护器也能在界面上补录。
   只有元器件汇总本身是只读的（它由上面三类推导而来）。
2. **程序告警和模型存疑分开。** 存疑项带 `source`（model / program），导出到 Excel 时
   分成两行，读回时不会把程序告警当成模型事实永久保留。
3. **坐标只用于定位，不参与计算。** 模型给不出 `bbox` 就留空，前端降级为纯表格模式
   并明确提示，不显示位置不可信的高亮框。多页图纸靠 `bbox.page` 决定去哪一页定位。

## 部署

- **本机/服务器**：`./run.sh` 即可；生产环境建议用 systemd 托管：
  `uvicorn app:app --host 0.0.0.0 --port 8000 --workers 1`（backend 目录下执行，
  先 `source ../.venv/bin/activate`）。
- **必须单 worker。** 任务表目前还在进程内存里，多 worker 之间不共享，轮询会拿到 404。
  另外进程在提取中被重启时，那个任务的状态会丢（PDF/PNG 会留在 work/ 下）。
  要上多 worker 或要求重启不丢任务，得先把任务表挑到 SQLite，这是下一步要做的。
- **反向代理**：Nginx 转发 8000 端口即可，前后端同源，无跨域问题。
- **说明**：任务为进程内后台执行，单机原型足够；任务完成后落盘为 `work/<id>.json`，
  服务重启后仍能打开历史项目。若需多用户并发或断点续传，后续可换成 Redis/RQ 或 Celery。

## 自定义

- 调整提取字段/口径：改 `backend/prompts/extract.txt` 与 `backend/extractor/schema.py`。
- 调整问答行为：改 `backend/prompts/assistant.txt`。
- 修改数量规则前先读 `backend/OUTPUT_CONTRACT.md`，并运行
  `cd backend && ../.venv/bin/python -m unittest discover -s tests -v`。
- 同图稳定性实测：`cd backend && ../.venv/bin/python stability_test.py 图纸.pdf --runs 3`
  （每次都会调用模型并计费）。
- 调整 Excel 版式：改 `backend/extractor/excel.py`，或在「设置」里指定模板 xlsx。
- 前端样式基准在 `frontend/app.css`，改 `:root` 变量即可整站换色。

## 费用与注意

- 每次提取按页调用视觉模型，AI 问答每次一条调用，均按所选模型计费；建议先用一张图试跑。
- API Key 只保存在服务端 `.env` 或 `backend/data/settings.json`，不经过前端。
