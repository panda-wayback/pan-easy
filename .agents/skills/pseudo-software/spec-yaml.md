# spec.yaml 规则

## 总则

1. 根目录与每个模块目录各有一份 `spec.yaml`：结构化接口，随契约更新。模板 [templates/spec.yaml](templates/spec.yaml)。
2. 产品方案与设计理由在 `docs/`，禁止写入 `spec.yaml`。
3. 改完 `spec.yaml` / 模块目录后运行 `python3 .cursor/skills/pseudo-software/scripts/check_index.py`；失败须修复。
4. 项目专属分层与业务禁令写在该仓库 `docs/` 或专属 rule；本 Skill 保持通用，禁止写死业务清单。

## 模块

- 模块 = 在父级 `spec.yaml` 的 `modules` 中登记的目录；根目录为顶层模块。
- 未登记的目录归属最近的已登记父模块，不单独写 `spec.yaml`。
- 需独立理解、独立测试、有明确边界的目录 → 登记为模块。

## 字段

| 字段 | 必填 | 内容 |
|---|---|---|
| `name` | 是 | 模块名 |
| `function` | 是 | 一句话职责 |
| `modules` | 是 | 子模块：`path`（相对本目录）+ `function`（一句话）；叶子写 `[]` |
| `input` | 是 | 列表：`name` / `type` / `desc`；无则 `[]` |
| `output` | 是 | 列表：`name` / `type` / `desc`；无则 `[]` |
| `contract` | 是，非空 | 行为保证、边界条件、不变式 |
| `depends` | 是 | 模块写仓库根相对路径；外部能力写 `ext:<名称>`；无则 `[]` |
| `errors` | 推荐 | 错误码、触发条件、返回值 |
| `tests` | 是，非空 | 独立测试策略；有子模块时另写组合测试、Integration Test |

根 `spec.yaml` 的 `modules` = 项目地图。

## 单一来源

- 父级 `modules` 只写 `path` + `function`。
- 输入/输出/契约/依赖/错误/测试只写在模块自己的 `spec.yaml`。
- 伪软件裁定后，模块契约以 `spec.yaml` 为准。

## 禁止

1. 写实现细节（函数级步骤、内部调用顺序、私有符号）。
2. 写设计理由或历史原因（写入 docs）。
3. 写产品方案。
