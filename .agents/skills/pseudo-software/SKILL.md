---
name: pseudo-software
description: >-
  伪软件：拆模块并落成 spec.yaml。触发：进入模块设计、改模块契约、写/审 spec.yaml。
---

# Pseudo Software

服从 `.cursor/rules/workflow.mdc`「方案先行」。本文件只写流程与调度；细节见下层文件。

## 流程

1. 读 docs 节点作为参考：`目标` / `要实现的` / `解决步骤`
2. 拆模块，在对话中给出伪软件草稿 → 用户裁定
3. 裁定后写入各模块 `spec.yaml`，登记父级 `modules`；`check_index.py` 通过

- 未裁定禁止写 `spec.yaml`。
- 契约需变更 → 回到第 2 步。

## 调度

| 步骤 | 读 |
|---|---|
| 1～3：拆模块、草稿、落 `spec.yaml` | `design.md` |
| `spec.yaml` 格式 | `spec-yaml.md` |
